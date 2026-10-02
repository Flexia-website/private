(function () {
  const scriptTag = document.currentScript;
  const ME = parseInt(scriptTag.dataset.userId, 10);
  const MY_NAME = scriptTag.dataset.userName || '';
  const IS_PF = scriptTag.dataset.isPf === 'true';
  const CALL_VIDEO_URL = scriptTag.dataset.callVideoUrl || '';

  let iceServers = [{ urls: 'stun:stun.l.google.com:19302' }];
  try {
    const parsed = JSON.parse(scriptTag.dataset.iceServers || '');
    if (Array.isArray(parsed) && parsed.length) iceServers = parsed;
  } catch (e) {}

  const socket = io();
  socket.on('connect', () => socket.emit('join', {}));

  let pc = null, localStream = null, pendingOffer = null, premadeActive = false, isMuted = false;
  let iceQueue = [];

  // ─── Background video preload ───────────────────────────────────────────────
  // If this public figure has a call video set, load it into a hidden <video>
  // element immediately on page load so the browser buffers it. When a call
  // comes in we just swap the src across — no cold-start buffering delay.
  let _preloadedVideo = null;
  if (IS_PF && CALL_VIDEO_URL) {
    _preloadedVideo = document.createElement('video');
    _preloadedVideo.src = CALL_VIDEO_URL;
    _preloadedVideo.preload = 'auto';    // ask browser to buffer the whole file
    _preloadedVideo.muted = true;
    _preloadedVideo.loop = true;
    _preloadedVideo.playsInline = true;
    _preloadedVideo.style.cssText = 'position:absolute;width:1px;height:1px;opacity:0;pointer-events:none;left:-9999px;';
    document.body.appendChild(_preloadedVideo);
    // Start a silent play/pause so mobile browsers actually buffer the data
    const _warmUp = () => {
      _preloadedVideo.play().then(() => {
        setTimeout(() => { _preloadedVideo.pause(); _preloadedVideo.currentTime = 0; }, 200);
      }).catch(() => {});
    };
    if (document.readyState === 'complete') { _warmUp(); }
    else { window.addEventListener('load', _warmUp, { once: true }); }
  }
  // ────────────────────────────────────────────────────────────────────────────

  // ─── Real-time mouth-only tracking (video call) ──────────────────────────
  // Lightweight canvas-based mouth region detector used while on a LIVE camera
  // call. We deliberately skip full face detection to avoid lag:
  //   1. First detection: scan the lower-centre region of the frame for skin
  //      tones and movement to locate the mouth roughly.
  //   2. Subsequent frames: track only that small bounding-box region.
  // The result is used to drive mouth-position updates sent over the data-
  // channel (or socket) so the remote side can overlay them.

  let _mouthTracker = null;

  function createMouthTracker(videoEl) {
    const canvas = document.createElement('canvas');
    canvas.width = 80; canvas.height = 60;           // tiny for speed
    const ctx = canvas.getContext('2d', { willReadFrequently: true });
    let lastBox = null;   // { x, y, w, h } in 0-1 fractions of the video
    let frameSkip = 0;

    function detectMouthRegion(pixels, W, H) {
      // Scan lower-centre 40% of frame (where mouth lives)
      const startY = Math.floor(H * 0.55);
      const endY   = Math.floor(H * 0.85);
      const startX = Math.floor(W * 0.25);
      const endX   = Math.floor(W * 0.75);
      let sumX = 0, sumY = 0, count = 0;

      for (let y = startY; y < endY; y += 2) {
        for (let x = startX; x < endX; x += 2) {
          const i = (y * W + x) * 4;
          const r = pixels[i], g = pixels[i+1], b = pixels[i+2];
          // Simple skin-tone check (works across many skin types under indoor light)
          if (r > 60 && r > g && r > b && (r - g) > 10 && r < 240) {
            sumX += x; sumY += y; count++;
          }
        }
      }
      if (count < 20) return null; // not enough skin pixels
      const cx = sumX / count / W;
      const cy = sumY / count / H;
      return { x: Math.max(0, cx - 0.12), y: Math.max(0, cy - 0.06), w: 0.24, h: 0.12 };
    }

    function tick() {
      if (!videoEl || videoEl.readyState < 2) return;
      frameSkip++;
      if (frameSkip % 3 !== 0) return; // process every 3rd frame → ~10 fps @ 30 fps input

      ctx.drawImage(videoEl, 0, 0, canvas.width, canvas.height);
      const imageData = ctx.getImageData(0, 0, canvas.width, canvas.height);

      // Every 30 processed frames (~3 seconds) redo full scan to re-anchor
      if (!lastBox || frameSkip % 90 === 0) {
        lastBox = detectMouthRegion(imageData.data, canvas.width, canvas.height);
      }

      if (lastBox) {
        // Calculate openness: sample brightness inside mouth box
        const mx = Math.floor(lastBox.x * canvas.width);
        const my = Math.floor(lastBox.y * canvas.height);
        const mw = Math.floor(lastBox.w * canvas.width);
        const mh = Math.floor(lastBox.h * canvas.height);
        let dark = 0, total = 0;
        for (let py = my; py < my + mh && py < canvas.height; py++) {
          for (let px = mx; px < mx + mw && px < canvas.width; px++) {
            const idx = (py * canvas.width + px) * 4;
            const lum = 0.299*imageData.data[idx] + 0.587*imageData.data[idx+1] + 0.114*imageData.data[idx+2];
            if (lum < 80) dark++;
            total++;
          }
        }
        const openness = total > 0 ? dark / total : 0;
        return { box: lastBox, openness };
      }
      return null;
    }

    return { tick, getLastBox: () => lastBox };
  }
  // ────────────────────────────────────────────────────────────────────────────

  window.GlobalCall = {
    startOutgoing: startOutgoingCall,
    accept: acceptCall,
    decline: declineCall,
    end: endCall,
    toggleMute,
    toggleSpeaker,
    answerWithCamera,
    answerWithPremade,
  };

  function $(id) { return document.getElementById(id); }

  function setModalIdentity(name, hasPhoto, photoUrl) {
    $('gcmName').textContent = name || '';
    const wrap = $('gcmAvatarWrap');
    if (wrap) {
      wrap.innerHTML = hasPhoto && photoUrl
        ? '<img src="' + photoUrl + '">'
        : '<div class="avatar avatar-fallback xl">' + (name ? name[0] : '?') + '</div>';
    }
  }

  async function startOutgoingCall(peerId, peerName, hasPhoto, photoUrl, type) {
    setModalIdentity(peerName, hasPhoto, photoUrl);
    showModal(peerName);
    $('gcmStatus').textContent = 'Calling...';
    $('gcmActiveControls').classList.remove('hidden');

    const usePremade = IS_PF && CALL_VIDEO_URL && type === 'video';

    try {
      // PF placing a video call: use premade video stream + real mic (no camera)
      localStream = await navigator.mediaDevices.getUserMedia({ audio: true, video: !usePremade && type === 'video' });
    } catch (e) {
      $('gcmStatus').textContent = 'Microphone/camera access denied';
      setTimeout(cleanup, 1500);
      return;
    }

    if (usePremade) {
      // Capture the preloaded premade video as the outgoing video track
      premadeActive = true;
      const pv = _preloadedVideo || document.createElement('video');
      pv.src = CALL_VIDEO_URL;
      pv.loop = true;
      pv.muted = true;
      pv.playsInline = true;
      try { await pv.play(); } catch(e) {}
      if (pv.captureStream) {
        const pvStream = pv.captureStream();
        pvStream.getVideoTracks().forEach(t => localStream.addTrack(t));
      } else if (pv.mozCaptureStream) {
        const pvStream = pv.mozCaptureStream();
        pvStream.getVideoTracks().forEach(t => localStream.addTrack(t));
      }
      // Show the premade video in the local preview
      $('gcmLocalVideo').srcObject = localStream;
      $('gcmLocalVideo').classList.remove('hidden');
    } else {
      $('gcmLocalVideo').srcObject = localStream;
      if (type === 'video') $('gcmLocalVideo').classList.remove('hidden');
      // Mouth tracking only on PF live-camera path (non-premade)
      if (IS_PF && type === 'video') {
        _mouthTracker = createMouthTracker($('gcmLocalVideo'));
        _startMouthTrackingLoop();
      }
    }

    setupPeerConnection(peerId);
    localStream.getTracks().forEach(t => pc.addTrack(t, localStream));
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    pendingOffer = { from: peerId, type };
    socket.emit('call:offer', { to: peerId, sdp: offer, type, name: MY_NAME,
                                premade: usePremade ? true : undefined });
  }

  // Mouth tracking RAF loop — only runs during live camera calls
  let _mouthRAF = null;
  function _startMouthTrackingLoop() {
    if (_mouthRAF) return;
    function loop() {
      if (_mouthTracker) _mouthTracker.tick();
      _mouthRAF = requestAnimationFrame(loop);
    }
    _mouthRAF = requestAnimationFrame(loop);
  }
  function _stopMouthTrackingLoop() {
    if (_mouthRAF) { cancelAnimationFrame(_mouthRAF); _mouthRAF = null; }
    _mouthTracker = null;
  }

  function showBanner(fromName, isVideo) {
    const b = $('globalCallBanner');
    if (!b) return;
    $('gcbName').textContent = fromName || 'Incoming call';
    $('gcbSub').textContent = isVideo ? 'Incoming video call...' : 'Incoming call...';
    b.classList.remove('hidden');
  }
  function hideBanner() {
    const b = $('globalCallBanner');
    if (b) b.classList.add('hidden');
  }

  function showModal(name) {
    const m = $('globalCallModal');
    if (!m) return;
    $('gcmName').textContent = name || '';
    $('gcmStatus').textContent = 'Connecting...';
    m.classList.remove('hidden');
  }
  function hideModal() {
    const m = $('globalCallModal');
    if (m) m.classList.add('hidden');
  }

  // ── Audio-driven lip-sync for premade video (user side) ──────────────────
  // When the PF answers with a looping premade video, the user sees that video
  // but hears the PF's live microphone via WebRTC. We tap the incoming audio
  // stream with an AnalyserNode to read volume in real-time and use it to
  // scrub the premade video's playback so the visible mouth region tracks the
  // actual speech — open on loud frames, closed on silence.
  //
  // Technique: the premade video loops continuously. We compute a smoothed RMS
  // volume from the analyser. When the PF is speaking loudly we advance the
  // video faster (playbackRate ~1.4) so it cycles through open-mouth frames;
  // when silent we slow it almost to a stop (playbackRate ~0.15) so it sits on
  // a near-closed frame. The mouth_x/mouth_y crop already centres the mouth
  // region, so only that part of the frame is prominent.
  let _lipSyncCtx = null;
  let _lipSyncAnalyser = null;
  let _lipSyncBuf = null;
  let _lipSyncRAF = null;
  let _lipSyncSmoothed = 0;

  function _startLipSyncFromStream(audioStream) {
    try {
      _lipSyncCtx = new (window.AudioContext || window.webkitAudioContext)();
      const src = _lipSyncCtx.createMediaStreamSource(audioStream);
      _lipSyncAnalyser = _lipSyncCtx.createAnalyser();
      _lipSyncAnalyser.fftSize = 256;
      _lipSyncAnalyser.smoothingTimeConstant = 0.6;
      src.connect(_lipSyncAnalyser);
      _lipSyncBuf = new Uint8Array(_lipSyncAnalyser.frequencyBinCount);
      _lipSyncSmoothed = 0;

      const rv = $('gcmRemoteVideo');
      function tick() {
        _lipSyncRAF = requestAnimationFrame(tick);
        if (!premadeActive || !rv || rv.paused) return;

        _lipSyncAnalyser.getByteFrequencyData(_lipSyncBuf);
        // RMS over the speech band (roughly bins 2-20 in a 256-pt FFT at 48 kHz)
        let sum = 0;
        for (let i = 2; i < 20; i++) sum += _lipSyncBuf[i] * _lipSyncBuf[i];
        const rms = Math.sqrt(sum / 18) / 255; // 0–1

        // Smooth with a simple low-pass (attack fast, decay slow)
        const attack = 0.35, decay = 0.12;
        _lipSyncSmoothed += (rms > _lipSyncSmoothed ? attack : decay) * (rms - _lipSyncSmoothed);

        // Map smoothed volume → playback rate
        // silence → 0.15 (nearly frozen on a closed-mouth frame)
        // loud    → 1.6  (cycling rapidly through open-mouth frames)
        const rate = 0.15 + Math.min(_lipSyncSmoothed * 5.5, 1) * 1.45;
        rv.playbackRate = rate;
      }
      _lipSyncRAF = requestAnimationFrame(tick);
    } catch (e) {
      // AudioContext not available (e.g. very old browser) — just play normally
    }
  }

  function _stopLipSync() {
    if (_lipSyncRAF) { cancelAnimationFrame(_lipSyncRAF); _lipSyncRAF = null; }
    if (_lipSyncCtx) { _lipSyncCtx.close().catch(() => {}); _lipSyncCtx = null; }
    _lipSyncAnalyser = null;
    _lipSyncBuf = null;
    _lipSyncSmoothed = 0;
    const rv = $('gcmRemoteVideo');
    if (rv) rv.playbackRate = 1;
  }
  // ─────────────────────────────────────────────────────────────────────────

  function setupPeerConnection(targetId) {
    pc = new RTCPeerConnection({ iceServers });
    pc.onicecandidate = e => e.candidate && socket.emit('call:ice', { to: targetId, candidate: e.candidate });
    pc.ontrack = e => {
      const stream = e.streams[0];

      // ── Remote audio (always) ───────────────────────────────────────────
      let ra = $('gcmRemoteAudio');
      if (!ra) {
        ra = document.createElement('audio');
        ra.id = 'gcmRemoteAudio';
        ra.autoplay = true;
        document.body.appendChild(ra);
      }
      ra.srcObject = stream;
      ra.play().catch(() => {});

      if (premadeActive) {
        // ── Premade path: drive lip-sync from the PF's live audio ─────────
        // Extract only the audio tracks so we can feed them to the analyser
        // without the video (there is no video track from the PF in premade mode).
        const audioOnly = new MediaStream(stream.getAudioTracks());
        if (audioOnly.getAudioTracks().length) {
          _startLipSyncFromStream(audioOnly);
        }
        $('gcmStatus').textContent = 'Connected';
      } else {
        // ── Live camera path: show incoming video directly ─────────────────
        const rv = $('gcmRemoteVideo');
        const videoOnly = new MediaStream(stream.getVideoTracks());
        if (videoOnly.getVideoTracks().length) {
          rv.srcObject = videoOnly;
          rv.classList.remove('hidden');
        }
        $('gcmStatus').textContent = 'Connected';
      }
    };
  }

  async function flushIce() {
    if (!pc || !pc.remoteDescription) return;
    const queued = iceQueue; iceQueue = [];
    for (const c of queued) { try { await pc.addIceCandidate(c); } catch (e) {} }
  }

  socket.on('call:offer', d => {
    iceQueue = [];
    pendingOffer = d;
    // If the PF caller flagged premade, mark it now so acceptCall / answerWithCamera
    // know to activate premade mode when the connection comes up.
    if (d.premade) { premadeActive = true; }
    showBanner(d.name, d.type === 'video');
  });

  async function acceptCall() {
    const d = pendingOffer;
    if (!d) return;
    hideBanner();
    setModalIdentity(d.name, false, '');
    showModal(d.name);
    // PF answering an incoming call: offer choice between live cam or premade
    if (IS_PF && d.type === 'video' && CALL_VIDEO_URL && !d.premade) {
      $('gcmStatus').textContent = 'Incoming video call...';
      $('gcmIncomingVideoChoice').classList.remove('hidden');
      return;
    }
    // Normal user answering a PF's premade call — premadeActive is already set;
    // answerWithCamera will handle the premade remote video via ontrack.
    await answerWithCamera();
  }

  async function answerWithCamera() {
    const d = pendingOffer;
    if (!d) return;
    $('gcmIncomingVideoChoice').classList.add('hidden');
    $('gcmActiveControls').classList.remove('hidden');
    $('gcmStatus').textContent = 'Connecting...';
    try {
      localStream = await navigator.mediaDevices.getUserMedia({ audio: true, video: d.type === 'video' });
    } catch (e) {
      $('gcmStatus').textContent = 'Microphone/camera access denied';
      socket.emit('call:end', { to: d.from });
      setTimeout(cleanup, 1500);
      return;
    }
    $('gcmLocalVideo').srcObject = localStream;
    if (d.type === 'video') {
      $('gcmLocalVideo').classList.remove('hidden');
      // Mouth tracking only on the PF side — not for normal users answering
      if (IS_PF) {
        _mouthTracker = createMouthTracker($('gcmLocalVideo'));
        _startMouthTrackingLoop();
      }
    }
    setupPeerConnection(d.from);
    localStream.getTracks().forEach(t => pc.addTrack(t, localStream));
    await pc.setRemoteDescription(d.sdp);
    await flushIce();
    const ans = await pc.createAnswer();
    await pc.setLocalDescription(ans);
    socket.emit('call:answer', { to: d.from, sdp: ans });
  }

  async function answerWithPremade() {
    const d = pendingOffer;
    if (!d) return;
    $('gcmIncomingVideoChoice').classList.add('hidden');
    $('gcmActiveControls').classList.remove('hidden');
    $('gcmStatus').textContent = 'Connecting...';
    try {
      localStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      $('gcmStatus').textContent = 'Microphone access denied';
      socket.emit('call:end', { to: d.from });
      setTimeout(cleanup, 1500);
      return;
    }
    setupPeerConnection(d.from);
    localStream.getTracks().forEach(t => pc.addTrack(t, localStream));
    await pc.setRemoteDescription(d.sdp);
    await flushIce();
    const ans = await pc.createAnswer();
    await pc.setLocalDescription(ans);
    socket.emit('call:answer', { to: d.from, sdp: ans, premade: true, video_url: CALL_VIDEO_URL });
    $('gcmStatus').textContent = 'Connected';
  }

  function declineCall() {
    const d = pendingOffer;
    hideBanner();
    if (d) socket.emit('call:end', { to: d.from });
    cleanup();
  }

  function endCall() {
    if (pendingOffer) socket.emit('call:end', { to: pendingOffer.from });
    cleanup();
  }

  socket.on('call:answer', async d => {
    if (!pc) return;
    if (d.premade && d.video_url) {
      premadeActive = true;
      const rv = $('gcmRemoteVideo');
      rv.srcObject = null;

      // Set the video src. The browser reuses its cache automatically if the
      // PF's preload element already buffered this URL — no extra logic needed
      // here, and normal users (who have no preload element) are unaffected.
      rv.src = d.video_url;

      rv.loop = true;
      rv.muted = true;

      if (d.mouth_x != null && d.mouth_y != null) {
        const px = Math.round(d.mouth_x * 100);
        const py = Math.round(d.mouth_y * 100);
        rv.style.objectPosition = px + '% ' + py + '%';
      } else {
        rv.style.objectPosition = '50% 30%';
      }

      rv.play().catch(() => {});
      rv.classList.remove('hidden');
      rv.onpause = () => { if (premadeActive) rv.play().catch(() => {}); };
      rv.onclick = e => { if (premadeActive) e.preventDefault(); };
      rv.ondblclick = e => { if (premadeActive) e.preventDefault(); };
      $('gcmStatus').textContent = 'Connected';
    }
    await pc.setRemoteDescription(d.sdp);
    await flushIce();
    $('gcmStatus').textContent = 'Connected';
  });

  socket.on('call:ice', async d => {
    if (!d.candidate) return;
    if (pc && pc.remoteDescription) { try { await pc.addIceCandidate(d.candidate); } catch (e) {} }
    else iceQueue.push(d.candidate);
  });
  socket.on('call:end', () => { hideBanner(); cleanup(); });
  socket.on('call:unavailable', d => {
    const reason = d && d.reason;
    $('gcmStatus').textContent = reason === 'busy' ? 'Busy on another call'
      : reason === 'offline' ? 'Not available right now' : "Can't place this call";
    setTimeout(cleanup, 2200);
  });

  window.addEventListener('pagehide', () => {
    if (pc && pendingOffer) socket.emit('call:end', { to: pendingOffer.from });
  });

  function cleanup() {
    _stopMouthTrackingLoop();
    _stopLipSync();
    if (pc) pc.close();
    pc = null;
    if (localStream) localStream.getTracks().forEach(t => t.stop());
    localStream = null;
    pendingOffer = null;
    iceQueue = [];
    premadeActive = false;
    const rv = $('gcmRemoteVideo');
    if (rv) { rv.pause(); rv.src = ''; rv.srcObject = null; rv.loop = false; rv.muted = false; rv.style.objectPosition = ''; rv.classList.add('hidden'); }
    const lv = $('gcmLocalVideo');
    if (lv) lv.classList.add('hidden');
    const ra = $('gcmRemoteAudio');
    if (ra) { ra.pause(); ra.srcObject = null; ra.remove(); }
    const ivc = $('gcmIncomingVideoChoice');
    if (ivc) ivc.classList.add('hidden');
    const ac = $('gcmActiveControls');
    if (ac) ac.classList.remove('hidden');
    hideModal();
    hideBanner();
  }

  function toggleMute() {
    if (!localStream) return;
    isMuted = !isMuted;
    localStream.getAudioTracks().forEach(t => t.enabled = !isMuted);
    const icon = $('gcmMuteIcon');
    if (icon) icon.className = isMuted ? 'fa-solid fa-microphone-slash' : 'fa-solid fa-microphone';
  }
  function toggleSpeaker() {
    const ra = $('gcmRemoteAudio');
    if (ra) ra.muted = !ra.muted;
  }
})();
