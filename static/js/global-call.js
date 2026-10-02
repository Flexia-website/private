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
  let iceQueue = [];  // candidates that arrive before we have a remote description

  // Let a chat.html page on the same tab claim ownership of an in-progress call
  // so the two UIs don't fight over the same <video>/<audio> elements.
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
    try {
      localStream = await navigator.mediaDevices.getUserMedia({ audio: true, video: type === 'video' });
    } catch (e) {
      $('gcmStatus').textContent = 'Microphone/camera access denied';
      setTimeout(cleanup, 1500);
      return;
    }
    $('gcmLocalVideo').srcObject = localStream;
    if (type === 'video') $('gcmLocalVideo').classList.remove('hidden');
    setupPeerConnection(peerId);
    localStream.getTracks().forEach(t => pc.addTrack(t, localStream));
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    pendingOffer = { from: peerId, type }; // track who we're calling, for end/cleanup
    socket.emit('call:offer', { to: peerId, sdp: offer, type, name: MY_NAME });
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

  function setupPeerConnection(targetId) {
    pc = new RTCPeerConnection({ iceServers });
    pc.onicecandidate = e => e.candidate && socket.emit('call:ice', { to: targetId, candidate: e.candidate });
    pc.ontrack = e => {
      let ra = $('gcmRemoteAudio');
      if (!ra) {
        ra = document.createElement('audio');
        ra.id = 'gcmRemoteAudio';
        ra.autoplay = true;
        document.body.appendChild(ra);
      }
      ra.srcObject = e.streams[0];
      ra.play().catch(() => {});
      if (!premadeActive) {
        const rv = $('gcmRemoteVideo');
        const videoOnly = new MediaStream(e.streams[0].getVideoTracks());
        if (videoOnly.getVideoTracks().length) {
          rv.srcObject = videoOnly;
          rv.classList.remove('hidden');
        }
      }
      $('gcmStatus').textContent = 'Connected';
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
    showBanner(d.name, d.type === 'video');
  });

  async function acceptCall() {
    const d = pendingOffer;
    if (!d) return;
    hideBanner();
    setModalIdentity(d.name, false, '');
    showModal(d.name);
    if (IS_PF && d.type === 'video' && CALL_VIDEO_URL) {
      // Let the public figure choose how to answer this specific video call
      $('gcmStatus').textContent = 'Incoming video call...';
      $('gcmIncomingVideoChoice').classList.remove('hidden');
      return;
    }
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
    if (d.type === 'video') $('gcmLocalVideo').classList.remove('hidden');
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
      rv.src = d.video_url;
      rv.loop = true;
      rv.muted = true;

      // If the PF mapped their mouth position, pan the video so it's centred
      if (d.mouth_x != null && d.mouth_y != null) {
        // object-position lets us shift the cover crop so the face is centred
        // mouth_x/mouth_y are 0–1 fractions; CSS object-position wants %
        const px = Math.round(d.mouth_x * 100);
        const py = Math.round(d.mouth_y * 100);
        rv.style.objectPosition = px + '% ' + py + '%';
      } else {
        rv.style.objectPosition = '50% 30%'; // default: upper-centre (face area)
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

  // This is a multi-page app: leaving the page kills the connection, so hang up properly
  // instead of leaving the other person on a dead call.
  window.addEventListener('pagehide', () => {
    if (pc && pendingOffer) socket.emit('call:end', { to: pendingOffer.from });
  });

  function cleanup() {
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
