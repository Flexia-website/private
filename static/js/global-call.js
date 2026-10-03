/**
 * global-call.js — WebRTC call handler with MediaPipe FaceMesh mouth overlay
 *
 * Architecture (premade video call path):
 *   PF side  : opens webcam → MediaPipe FaceMesh → crops mouth landmarks each frame
 *              → paints onto a tiny canvas → captureStream(15) → extra WebRTC video track
 *   Fan side : receives premade video URL (plays full face/body) + tiny mouth track
 *              → MediaPipe FaceMesh on the PREMADE video → finds mouth destination per frame
 *              → composites live mouth patch at the tracked position with:
 *                  - skin-ring colour matching (perimeter skin only, lips excluded)
 *                  - per-pixel lip detection (reddish pixels skipped from correction)
 *                  - feathered ellipse blend
 *
 * MediaPipe FaceMesh CDN (loaded in base.html before this script):
 *   @mediapipe/face_mesh  0.4.1633559619
 *   @mediapipe/camera_utils 0.3.1632090217
 *   @mediapipe/drawing_utils 0.3.1620248257
 *
 * Landmark indices used (from the 468-point mesh):
 *   Outer lip ring : 61,146,91,181,84,17,314,405,321,375,291,
 *                    308,324,318,402,317,14,87,178,88,95
 */
(function () {
  const scriptTag      = document.currentScript;
  const ME             = parseInt(scriptTag.dataset.userId, 10);
  const MY_NAME        = scriptTag.dataset.userName || '';
  const IS_PF          = scriptTag.dataset.isPf === 'true';
  const CALL_VIDEO_URL = scriptTag.dataset.callVideoUrl || '';
  const PF_MOUTH_X     = parseFloat(scriptTag.dataset.mouthX)  || 0.5;
  const PF_MOUTH_Y     = parseFloat(scriptTag.dataset.mouthY)  || 0.35;

  let iceServers = [{ urls: 'stun:stun.l.google.com:19302' }];
  try {
    const parsed = JSON.parse(scriptTag.dataset.iceServers || '');
    if (Array.isArray(parsed) && parsed.length) iceServers = parsed;
  } catch (e) {}

  const socket = io();
  socket.on('connect', () => socket.emit('join', {}));

  let pc = null, localStream = null, pendingOffer = null;
  let premadeActive = false, isMuted = false;
  let iceQueue = [];

  // ── MediaPipe lip landmark indices (outer contour) ────────────────────────
  const LIP_IDX = [
    61,146,91,181,84,17,314,405,321,375,291,
    308,324,318,402,317,14,87,178,88,95
  ];
  const LIP_PAD_X = 0.04;
  const LIP_PAD_Y = 0.025;

  // ── Mouth canvas dimensions sent over WebRTC ──────────────────────────────
  const MOUTH_W  = 220;
  const MOUTH_H  = 110;
  const FEATHER  = 18;   // px — feather radius on the fan compositor

  // ── Colour-match tuning ───────────────────────────────────────────────────
  // Ring width (px) sampled around the destination/patch box for skin colour
  const SKIN_RING = 10;
  // How much of the ratio correction to apply (0=none, 1=full).
  // 0.75 blends the correction with the original to avoid over-correction.
  const BLEND_STRENGTH = 0.75;
  // Correction scale clamped to this range per channel
  const RATIO_MIN = 0.55;
  const RATIO_MAX = 1.65;
  // A pixel is considered "lip" (and skipped from correction) when
  //   R - G > LIP_RG_THRESH  AND  R - B > LIP_RB_THRESH
  // Works across skin tones: what matters is relative red excess, not absolute.
  const LIP_RG_THRESH = 20;
  const LIP_RB_THRESH = 18;
  // Minimum alpha for a pixel to be included in skin sampling
  const ALPHA_MIN = 100;

  // ─── State: PF sender side ────────────────────────────────────────────────
  let _mouthCamStream = null;
  let _mouthCamVideo  = null;
  let _mouthCanvas    = null;
  let _mouthCtx       = null;
  let _mouthRAF       = null;
  let _pfFaceMesh     = null;
  let _pfLipBox       = null;

  // ─── State: fan receiver side ─────────────────────────────────────────────
  let _overlayCanvas  = null;
  let _overlayCtx     = null;
  let _overlayRAF     = null;
  let _mouthFeedVideo = null;
  let _fanFaceMesh    = null;
  let _fanLipBox      = null;

  // ─── Pending signal data ──────────────────────────────────────────────────
  let _pendingMouthX  = null;
  let _pendingMouthY  = null;
  let _hasMouthTrack  = false;

  // ─── Background premade video preload (PF side) ───────────────────────────
  let _preloadedVideo = null;
  if (IS_PF && CALL_VIDEO_URL) {
    _preloadedVideo = document.createElement('video');
    _preloadedVideo.src = CALL_VIDEO_URL;
    _preloadedVideo.preload = 'auto';
    _preloadedVideo.muted = true;
    _preloadedVideo.loop  = true;
    _preloadedVideo.playsInline = true;
    _preloadedVideo.style.cssText = 'position:absolute;width:1px;height:1px;opacity:0;pointer-events:none;left:-9999px;';
    document.body.appendChild(_preloadedVideo);
    const warmUp = () => {
      _preloadedVideo.play()
        .then(() => setTimeout(() => { _preloadedVideo.pause(); _preloadedVideo.currentTime = 0; }, 200))
        .catch(() => {});
    };
    if (document.readyState === 'complete') warmUp();
    else window.addEventListener('load', warmUp, { once: true });
  }

  // ═══════════════════════════════════════════════════════════════════════════
  //  UTILITY
  // ═══════════════════════════════════════════════════════════════════════════

  function clamp(v, lo, hi) { return v < lo ? lo : v > hi ? hi : v; }

  /**
   * Build a MediaPipe FaceMesh instance.
   * locateFile redirects WASM fetches to jsDelivr — required on Render because
   * the binary is not served from the app's own origin.
   */
  function _buildFaceMesh(onResults) {
    if (typeof FaceMesh === 'undefined') return null;
    const fm = new FaceMesh({
      locateFile: (file) =>
        `https://cdn.jsdelivr.net/npm/@mediapipe/face_mesh@0.4.1633559619/${file}`
    });
    fm.setOptions({
      maxNumFaces:            1,
      refineLandmarks:        true,
      minDetectionConfidence: 0.5,
      minTrackingConfidence:  0.5,
    });
    fm.onResults(onResults);
    return fm;
  }

  /**
   * Compute pixel bounding box of the outer lip landmarks.
   * Returns {x, y, w, h} in pixels, with padding.  Null if no landmarks.
   */
  function _lipBoundingBox(landmarks, W, H) {
    if (!landmarks || !landmarks.length) return null;
    let minX = 1, minY = 1, maxX = 0, maxY = 0;
    for (const idx of LIP_IDX) {
      const lm = landmarks[idx]; if (!lm) continue;
      if (lm.x < minX) minX = lm.x; if (lm.y < minY) minY = lm.y;
      if (lm.x > maxX) maxX = lm.x; if (lm.y > maxY) maxY = lm.y;
    }
    const px = (minX - LIP_PAD_X) * W, py = (minY - LIP_PAD_Y) * H;
    const pw = (maxX - minX + LIP_PAD_X * 2) * W;
    const ph = (maxY - minY + LIP_PAD_Y * 2) * H;
    return {
      x: Math.max(0, px), y: Math.max(0, py),
      w: Math.min(pw, W - Math.max(0, px)),
      h: Math.min(ph, H - Math.max(0, py)),
    };
  }

  // ═══════════════════════════════════════════════════════════════════════════
  //  COLOUR MATCHING  (skin-ring approach, lip-aware)
  // ═══════════════════════════════════════════════════════════════════════════

  /**
   * Sample average skin colour from a ring of pixels around a box within a
   * canvas context.  Only pixels that pass the skin-tone heuristic are counted
   * (R > G, R > B, not too dark, not saturated-red lip colour).
   * Returns [R, G, B] averages or null if fewer than 20 qualifying pixels found.
   *
   * @param {CanvasRenderingContext2D} ctx
   * @param {Object}  box   {x, y, w, h} in canvas pixel coords
   * @param {number}  cW    canvas width
   * @param {number}  cH    canvas height
   * @param {number}  ring  ring thickness in px
   */
  function _sampleSkinRing(ctx, box, cW, cH, ring) {
    // Build the four perimeter strips: top, bottom, left, right
    const strips = [
      // top strip (above the box)
      { x: box.x - ring, y: box.y - ring, w: box.w + ring * 2, h: ring },
      // bottom strip
      { x: box.x - ring, y: box.y + box.h, w: box.w + ring * 2, h: ring },
      // left strip (between top and bottom strips)
      { x: box.x - ring, y: box.y, w: ring, h: box.h },
      // right strip
      { x: box.x + box.w, y: box.y, w: ring, h: box.h },
    ];

    let sumR = 0, sumG = 0, sumB = 0, n = 0;

    for (const s of strips) {
      const sx = Math.max(0, Math.round(s.x));
      const sy = Math.max(0, Math.round(s.y));
      const sw = Math.min(Math.round(s.w), cW - sx);
      const sh = Math.min(Math.round(s.h), cH - sy);
      if (sw <= 0 || sh <= 0) continue;

      let imgData;
      try { imgData = ctx.getImageData(sx, sy, sw, sh); } catch(e) { continue; }
      const d = imgData.data;

      for (let i = 0; i < d.length; i += 4) {
        const r = d[i], g = d[i+1], b = d[i+2], a = d[i+3];
        if (a < ALPHA_MIN) continue;
        // Skin heuristic: R is dominant, not too dark, not washed out
        if (r < 50 || r > 240) continue;          // too dark or blown out
        if (r <= g || r <= b)  continue;           // not warm enough
        // Exclude lip-coloured pixels (strong red excess over green)
        if ((r - g) > LIP_RG_THRESH && (r - b) > LIP_RB_THRESH) continue;
        sumR += r; sumG += g; sumB += b; n++;
      }
    }

    return n >= 20 ? [sumR / n, sumG / n, sumB / n] : null;
  }

  /**
   * Apply complexion-aware colour correction to a patch canvas in-place.
   *
   * Strategy:
   *   1. Sample skin colour ring around the destination box in the premade frame
   *      (overlayCtx) — this is the "target" complexion.
   *   2. Sample skin colour ring around the patch interior in the patch canvas
   *      (tmpCtx) — this is the "source" complexion.
   *   3. Compute per-channel scale factor, clamped to [RATIO_MIN, RATIO_MAX].
   *   4. Apply correction per-pixel to the patch, SKIPPING lip-coloured pixels
   *      so we don't bleach or discolour the lips themselves.
   *   5. Blend the correction with the original at BLEND_STRENGTH so
   *      over-correction is damped.
   *
   * Works across large complexion differences because:
   *   - We match skin-to-skin (not patch-average-to-premade-average).
   *   - Lip pixels are excluded from both sampling and correction.
   *   - The blend strength prevents extreme shifts.
   *
   * @param {CanvasRenderingContext2D} tmpCtx     patch canvas context (MOUTH_W × MOUTH_H)
   * @param {CanvasRenderingContext2D} overlayCtx main overlay canvas context
   * @param {Object} destBox  {x, y, w, h} destination in the overlay canvas (pixels)
   * @param {number} dw       overlay canvas width
   * @param {number} dh       overlay canvas height
   */
  function _applyColourMatch(tmpCtx, overlayCtx, destBox, dw, dh) {
    // 1. Target skin: ring around destination in the premade frame
    const skinDest  = _sampleSkinRing(overlayCtx, destBox, dw, dh, SKIN_RING);
    // 2. Source skin: ring around the patch content (inner box, excluding feather margin)
    const patchBox  = { x: FEATHER, y: FEATHER, w: MOUTH_W - FEATHER * 2, h: MOUTH_H - FEATHER * 2 };
    const skinPatch = _sampleSkinRing(tmpCtx, patchBox, MOUTH_W, MOUTH_H, SKIN_RING);

    // If either sample failed (low contrast scene, edge of frame, etc.) bail out
    if (!skinDest || !skinPatch) return;

    // 3. Per-channel ratio, clamped
    const rr = clamp(skinDest[0] / (skinPatch[0] || 1), RATIO_MIN, RATIO_MAX);
    const gr = clamp(skinDest[1] / (skinPatch[1] || 1), RATIO_MIN, RATIO_MAX);
    const br = clamp(skinDest[2] / (skinPatch[2] || 1), RATIO_MIN, RATIO_MAX);

    // Early exit: if ratios are all very close to 1 there's nothing to correct
    if (Math.abs(rr-1) < 0.04 && Math.abs(gr-1) < 0.04 && Math.abs(br-1) < 0.04) return;

    // 4 & 5. Per-pixel correction with lip exclusion and blend damping
    let imgData;
    try { imgData = tmpCtx.getImageData(0, 0, MOUTH_W, MOUTH_H); } catch(e) { return; }
    const d = imgData.data;
    const bs = BLEND_STRENGTH;
    const bi = 1 - bs;

    for (let i = 0; i < d.length; i += 4) {
      const r = d[i], g = d[i+1], b = d[i+2], a = d[i+3];
      if (a < ALPHA_MIN) continue;

      // Skip lip-coloured pixels — preserve their natural hue
      const isLip = (r - g) > LIP_RG_THRESH && (r - b) > LIP_RB_THRESH;
      if (isLip) continue;

      // Apply blended correction
      d[i]   = clamp(Math.round(r * rr * bs + r * bi), 0, 255);
      d[i+1] = clamp(Math.round(g * gr * bs + g * bi), 0, 255);
      d[i+2] = clamp(Math.round(b * br * bs + b * bi), 0, 255);
    }
    tmpCtx.putImageData(imgData, 0, 0);
  }

  // ═══════════════════════════════════════════════════════════════════════════
  //  PF SENDER: webcam → FaceMesh → crop → captureStream
  // ═══════════════════════════════════════════════════════════════════════════

  async function _startMouthCropStream() {
    try {
      _mouthCamStream = await navigator.mediaDevices.getUserMedia({
        video: { width: { ideal: 640 }, height: { ideal: 480 }, facingMode: 'user' },
        audio: false,
      });
    } catch (e) {
      console.warn('[mouth-overlay] webcam denied — mouth overlay disabled', e);
      return null;
    }

    _mouthCamVideo = document.createElement('video');
    _mouthCamVideo.srcObject = _mouthCamStream;
    _mouthCamVideo.muted = true;
    _mouthCamVideo.playsInline = true;
    _mouthCamVideo.style.cssText = 'position:absolute;width:1px;height:1px;opacity:0;pointer-events:none;left:-9999px;';
    document.body.appendChild(_mouthCamVideo);
    await _mouthCamVideo.play();

    _mouthCanvas = document.createElement('canvas');
    _mouthCanvas.width  = MOUTH_W;
    _mouthCanvas.height = MOUTH_H;
    _mouthCtx = _mouthCanvas.getContext('2d', { willReadFrequently: false });

    _pfFaceMesh = _buildFaceMesh((results) => {
      if (!results.multiFaceLandmarks || !results.multiFaceLandmarks[0]) return;
      const vw = _mouthCamVideo.videoWidth  || 640;
      const vh = _mouthCamVideo.videoHeight || 480;
      _pfLipBox = _lipBoundingBox(results.multiFaceLandmarks[0], vw, vh);
    });

    if (!_pfFaceMesh) {
      console.warn('[mouth-overlay] MediaPipe not loaded — using fixed crop fallback');
      _startFixedCropLoop();
      return _captureStream(_mouthCanvas);
    }

    let fmTick = 0;
    async function cropLoop() {
      _mouthRAF = requestAnimationFrame(cropLoop);
      if (!_mouthCamVideo || _mouthCamVideo.readyState < 2) return;
      fmTick++;
      if (fmTick % 3 === 0 && _pfFaceMesh) {
        try { await _pfFaceMesh.send({ image: _mouthCamVideo }); } catch(e) {}
      }
      _paintMouthToCanvas(_mouthCamVideo, _pfLipBox);
    }
    cropLoop();

    return _captureStream(_mouthCanvas);
  }

  function _startFixedCropLoop() {
    async function loop() {
      _mouthRAF = requestAnimationFrame(loop);
      if (!_mouthCamVideo || _mouthCamVideo.readyState < 2) return;
      const vw = _mouthCamVideo.videoWidth  || 640;
      const vh = _mouthCamVideo.videoHeight || 480;
      const box = {
        x: (1 - PF_MOUTH_X) * vw - vw * 0.15,
        y: PF_MOUTH_Y * vh  - vh * 0.10,
        w: vw * 0.30, h: vh * 0.20,
      };
      _paintMouthToCanvas(_mouthCamVideo, box);
    }
    loop();
  }

  function _paintMouthToCanvas(srcVideo, box) {
    if (!_mouthCtx || !srcVideo || srcVideo.readyState < 2) return;
    const vw = srcVideo.videoWidth  || 640;
    const vh = srcVideo.videoHeight || 480;
    let sx, sy, sw, sh;
    if (box) {
      sx = box.x; sy = box.y; sw = box.w; sh = box.h;
    } else {
      sw = vw * 0.30; sh = vh * 0.20;
      sx = (1 - PF_MOUTH_X) * vw - sw / 2;
      sy = PF_MOUTH_Y * vh  - sh / 2;
    }
    sx = Math.max(0, Math.min(sx, vw - sw));
    sy = Math.max(0, Math.min(sy, vh - sh));
    _mouthCtx.save();
    // De-mirror the selfie feed
    _mouthCtx.translate(MOUTH_W, 0);
    _mouthCtx.scale(-1, 1);
    _mouthCtx.drawImage(srcVideo, sx, sy, sw, sh, 0, 0, MOUTH_W, MOUTH_H);
    _mouthCtx.restore();
  }

  function _captureStream(canvas) {
    if (canvas.captureStream)      return canvas.captureStream(15);
    if (canvas.mozCaptureStream)   return canvas.mozCaptureStream(15);
    return null;
  }

  function _stopMouthCropStream() {
    if (_mouthRAF)       { cancelAnimationFrame(_mouthRAF); _mouthRAF = null; }
    if (_pfFaceMesh)     { _pfFaceMesh.close(); _pfFaceMesh = null; }
    if (_mouthCamStream) { _mouthCamStream.getTracks().forEach(t => t.stop()); _mouthCamStream = null; }
    if (_mouthCamVideo)  { _mouthCamVideo.srcObject = null; _mouthCamVideo.remove(); _mouthCamVideo = null; }
    _mouthCanvas = null; _mouthCtx = null; _pfLipBox = null;
  }

  // ═══════════════════════════════════════════════════════════════════════════
  //  FAN RECEIVER: composite premade + tracked mouth + colour match
  // ═══════════════════════════════════════════════════════════════════════════

  function _startMouthOverlay(premadeVideoEl, mouthTrack, fallbackMX, fallbackMY) {
    // 1. Hidden video for the incoming mouth WebRTC track
    _mouthFeedVideo = document.createElement('video');
    _mouthFeedVideo.srcObject = new MediaStream([mouthTrack]);
    _mouthFeedVideo.muted = true; _mouthFeedVideo.playsInline = true; _mouthFeedVideo.autoplay = true;
    _mouthFeedVideo.style.cssText = 'position:absolute;width:1px;height:1px;opacity:0;pointer-events:none;left:-9999px;';
    document.body.appendChild(_mouthFeedVideo);
    _mouthFeedVideo.play().catch(() => {});

    // 2. FaceMesh on the premade video to track destination lip box per frame
    _fanFaceMesh = _buildFaceMesh((results) => {
      if (!results.multiFaceLandmarks || !results.multiFaceLandmarks[0]) {
        _fanLipBox = null; return;
      }
      const dw = premadeVideoEl.videoWidth  || (_overlayCanvas && _overlayCanvas.width)  || 640;
      const dh = premadeVideoEl.videoHeight || (_overlayCanvas && _overlayCanvas.height) || 480;
      _fanLipBox = _lipBoundingBox(results.multiFaceLandmarks[0], dw, dh);
    });

    // 3. Overlay canvas injected before the premade video element
    _overlayCanvas = document.createElement('canvas');
    _overlayCanvas.className = premadeVideoEl.className;
    _overlayCanvas.style.cssText = `
      position:absolute;inset:0;width:100%;height:100%;
      object-fit:cover;display:block;z-index:1;
    `;
    premadeVideoEl.parentNode.insertBefore(_overlayCanvas, premadeVideoEl);
    premadeVideoEl.style.opacity = '0';
    premadeVideoEl.style.pointerEvents = 'none';
    _overlayCtx = _overlayCanvas.getContext('2d', { willReadFrequently: true });

    // 4. Compositor RAF loop
    let fmTick = 0;
    async function draw() {
      _overlayRAF = requestAnimationFrame(draw);
      if (!premadeVideoEl || premadeVideoEl.readyState < 2) return;

      const dw = premadeVideoEl.videoWidth;
      const dh = premadeVideoEl.videoHeight;
      if (!dw || !dh) return;

      if (_overlayCanvas.width !== dw || _overlayCanvas.height !== dh) {
        _overlayCanvas.width = dw; _overlayCanvas.height = dh;
      }

      // Send premade frame to FaceMesh every 2nd tick
      fmTick++;
      if (fmTick % 2 === 0 && _fanFaceMesh) {
        try { await _fanFaceMesh.send({ image: premadeVideoEl }); } catch(e) {}
      }

      // Draw full premade frame
      _overlayCtx.drawImage(premadeVideoEl, 0, 0, dw, dh);

      if (!_mouthFeedVideo || _mouthFeedVideo.readyState < 2) return;

      // Resolve destination lip box
      let destBox = _fanLipBox;
      if (!destBox) {
        const fw = fallbackMX != null ? fallbackMX : 0.5;
        const fh = fallbackMY != null ? fallbackMY : 0.35;
        const bw = dw * 0.28, bh = dh * 0.14;
        destBox = { x: fw * dw - bw / 2, y: fh * dh - bh / 2, w: bw, h: bh };
      }

      // ── Build patch on a temporary canvas ──────────────────────────────
      // Sized to include the feather margin so the mask gradient has room
      const patchW = Math.ceil(destBox.w + FEATHER * 2);
      const patchH = Math.ceil(destBox.h + FEATHER * 2);
      const tmp    = document.createElement('canvas');
      tmp.width    = patchW;
      tmp.height   = patchH;
      const tmpCtx = tmp.getContext('2d', { willReadFrequently: true });

      // Draw incoming mouth frame into the centre of the tmp canvas
      // (FEATHER-px inset so the gradient covers the real boundary)
      tmpCtx.drawImage(_mouthFeedVideo, FEATHER, FEATHER, destBox.w, destBox.h);

      // ── Complexion-aware colour correction ─────────────────────────────
      // Must run BEFORE the feather mask so we only correct actual content pixels.
      _applyColourMatch(tmpCtx, _overlayCtx, destBox, dw, dh);

      // ── Feathered ellipse mask ──────────────────────────────────────────
      // Use destination-in with a radial gradient to create a soft elliptical
      // alpha channel on the patch.  We scale the canvas context so the
      // gradient is always circular in a normalised space (avoids ellipse
      // distortion artefacts in createRadialGradient).
      tmpCtx.globalCompositeOperation = 'destination-in';
      const cx = patchW / 2;
      const cy = patchH / 2;
      const rx = destBox.w / 2 + FEATHER * 0.4;
      const ry = destBox.h / 2 + FEATHER * 0.4;
      tmpCtx.save();
      // Squish to circle in the Y axis for the gradient, then undo after fill
      const scaleY = ry / rx;
      tmpCtx.scale(1, scaleY);
      const grad = tmpCtx.createRadialGradient(cx, cy / scaleY, 0, cx, cy / scaleY, rx);
      grad.addColorStop(0,    'rgba(0,0,0,1)');
      grad.addColorStop(0.60, 'rgba(0,0,0,1)');
      grad.addColorStop(1,    'rgba(0,0,0,0)');
      tmpCtx.fillStyle = grad;
      tmpCtx.fillRect(0, 0, patchW, patchH / scaleY);
      tmpCtx.restore();

      // ── Blit patch onto the overlay canvas ─────────────────────────────
      _overlayCtx.drawImage(tmp, destBox.x - FEATHER, destBox.y - FEATHER);
    }
    draw();
  }

  function _stopMouthOverlay() {
    if (_overlayRAF)    { cancelAnimationFrame(_overlayRAF); _overlayRAF = null; }
    if (_fanFaceMesh)   { _fanFaceMesh.close(); _fanFaceMesh = null; }
    if (_overlayCanvas) { _overlayCanvas.remove(); _overlayCanvas = null; }
    _overlayCtx = null; _fanLipBox = null;
    if (_mouthFeedVideo) { _mouthFeedVideo.srcObject = null; _mouthFeedVideo.remove(); _mouthFeedVideo = null; }
    const rv = $('gcmRemoteVideo');
    if (rv) { rv.style.opacity = ''; rv.style.pointerEvents = ''; }
  }

  // ═══════════════════════════════════════════════════════════════════════════
  //  AUDIO-DRIVEN LIP-SYNC (controls premade video play/pause to keep head
  //  movement natural even while the mouth region is overlaid)
  // ═══════════════════════════════════════════════════════════════════════════

  let _lipSyncCtx = null, _lipSyncAnalyser = null, _lipSyncBuf = null;
  let _lipSyncRAF = null, _lipSyncSmoothed = 0, _lipSyncSpeaking = false;
  const SILENCE_THRESHOLD = 0.04;
  const SPEAK_THRESHOLD   = 0.07;

  function _startLipSyncFromStream(audioStream) {
    try {
      _lipSyncCtx = new (window.AudioContext || window.webkitAudioContext)();
      const src = _lipSyncCtx.createMediaStreamSource(audioStream);
      _lipSyncAnalyser = _lipSyncCtx.createAnalyser();
      _lipSyncAnalyser.fftSize = 512;
      _lipSyncAnalyser.smoothingTimeConstant = 0.5;
      src.connect(_lipSyncAnalyser);
      _lipSyncBuf = new Uint8Array(_lipSyncAnalyser.frequencyBinCount);
      _lipSyncSmoothed = 0; _lipSyncSpeaking = false;
      const rv = $('gcmRemoteVideo');
      let skip = 0;
      function tick() {
        _lipSyncRAF = requestAnimationFrame(tick);
        if (!premadeActive || !rv) return;
        if (++skip % 2 !== 0) return;
        _lipSyncAnalyser.getByteFrequencyData(_lipSyncBuf);
        let sum = 0;
        for (let i = 1; i < 18; i++) sum += _lipSyncBuf[i] * _lipSyncBuf[i];
        const rms = Math.sqrt(sum / 17) / 255;
        const alpha = rms > _lipSyncSmoothed ? 0.45 : 0.10;
        _lipSyncSmoothed += alpha * (rms - _lipSyncSmoothed);
        if (!_lipSyncSpeaking && _lipSyncSmoothed > SPEAK_THRESHOLD) {
          _lipSyncSpeaking = true;
          if (rv.paused) rv.play().catch(() => {});
          rv.playbackRate = 1.0;
        } else if (_lipSyncSpeaking && _lipSyncSmoothed < SILENCE_THRESHOLD) {
          _lipSyncSpeaking = false;
          if (!rv.paused) { rv.pause(); rv.currentTime = 0.05; }
        }
        if (_lipSyncSpeaking && !rv.paused)
          rv.playbackRate = 0.85 + Math.min(_lipSyncSmoothed * 3.0, 0.6);
      }
      _lipSyncRAF = requestAnimationFrame(tick);
    } catch(e) {}
  }

  function _stopLipSync() {
    if (_lipSyncRAF) { cancelAnimationFrame(_lipSyncRAF); _lipSyncRAF = null; }
    if (_lipSyncCtx) { _lipSyncCtx.close().catch(() => {}); _lipSyncCtx = null; }
    _lipSyncAnalyser = null; _lipSyncBuf = null;
    _lipSyncSmoothed = 0; _lipSyncSpeaking = false;
    const rv = $('gcmRemoteVideo');
    if (rv) { rv.playbackRate = 1; if (rv.paused && premadeActive) rv.play().catch(() => {}); }
  }

  // ═══════════════════════════════════════════════════════════════════════════
  //  WEBRTC + SIGNALLING
  // ═══════════════════════════════════════════════════════════════════════════

  window.GlobalCall = {
    startOutgoing: startOutgoingCall,
    accept:        acceptCall,
    decline:       declineCall,
    end:           endCall,
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
        ? `<img src="${photoUrl}">`
        : `<div class="avatar avatar-fallback xl">${name ? name[0] : '?'}</div>`;
    }
  }

  async function startOutgoingCall(peerId, peerName, hasPhoto, photoUrl, type) {
    setModalIdentity(peerName, hasPhoto, photoUrl);
    showModal(peerName);
    $('gcmStatus').textContent = 'Calling...';
    $('gcmActiveControls').classList.remove('hidden');

    const usePremade = IS_PF && CALL_VIDEO_URL && type === 'video';

    try {
      localStream = await navigator.mediaDevices.getUserMedia({
        audio: true, video: !usePremade && type === 'video',
      });
    } catch(e) {
      $('gcmStatus').textContent = 'Microphone/camera access denied';
      setTimeout(cleanup, 1500); return;
    }

    if (usePremade) {
      premadeActive = true;
      const pv = _preloadedVideo || document.createElement('video');
      pv.src = CALL_VIDEO_URL; pv.loop = true; pv.muted = true; pv.playsInline = true;
      try { await pv.play(); } catch(e) {}
      const pvCap = pv.captureStream ? pv.captureStream() : (pv.mozCaptureStream ? pv.mozCaptureStream() : null);
      if (pvCap) pvCap.getVideoTracks().forEach(t => localStream.addTrack(t));
      $('gcmLocalVideo').srcObject = localStream;
      $('gcmLocalVideo').classList.remove('hidden');

      const mouthStream = await _startMouthCropStream();
      if (mouthStream) mouthStream.getVideoTracks().forEach(t => localStream.addTrack(t));

      setupPeerConnection(peerId);
      localStream.getTracks().forEach(t => pc.addTrack(t, localStream));
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      pendingOffer = { from: peerId, type };
      socket.emit('call:offer', {
        to: peerId, sdp: offer, type, name: MY_NAME,
        premade: true,
        has_mouth_track: mouthStream ? true : undefined,
      });
    } else {
      $('gcmLocalVideo').srcObject = localStream;
      if (type === 'video') $('gcmLocalVideo').classList.remove('hidden');
      setupPeerConnection(peerId);
      localStream.getTracks().forEach(t => pc.addTrack(t, localStream));
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      pendingOffer = { from: peerId, type };
      socket.emit('call:offer', { to: peerId, sdp: offer, type, name: MY_NAME });
    }
  }

  function setupPeerConnection(targetId) {
    pc = new RTCPeerConnection({ iceServers });
    pc.onicecandidate = e => e.candidate && socket.emit('call:ice', { to: targetId, candidate: e.candidate });

    pc.ontrack = e => {
      const track  = e.track;
      const stream = e.streams[0];

      if (track.kind === 'audio') {
        let ra = $('gcmRemoteAudio');
        if (!ra) { ra = document.createElement('audio'); ra.id = 'gcmRemoteAudio'; ra.autoplay = true; document.body.appendChild(ra); }
        ra.srcObject = stream;
        ra.play().catch(() => {});
        if (premadeActive) _startLipSyncFromStream(new MediaStream([track]));
        return;
      }

      if (track.kind === 'video') {
        if (premadeActive && _hasMouthTrack) {
          const probe = document.createElement('video');
          probe.srcObject = new MediaStream([track]);
          probe.muted = true; probe.playsInline = true;
          probe.style.cssText = 'position:absolute;width:1px;height:1px;opacity:0;pointer-events:none;left:-9999px;';
          document.body.appendChild(probe);
          probe.play().catch(() => {});
          probe.addEventListener('loadedmetadata', () => {
            probe.remove();
            if (probe.videoWidth <= MOUTH_W * 2) {
              const rv = $('gcmRemoteVideo');
              if (rv) _startMouthOverlay(rv, track, _pendingMouthX, _pendingMouthY);
            }
          }, { once: true });
        } else if (!premadeActive) {
          const rv = $('gcmRemoteVideo');
          rv.srcObject = new MediaStream([track]);
          rv.classList.remove('hidden');
        }
        $('gcmStatus').textContent = 'Connected';
      }
    };
  }

  async function flushIce() {
    if (!pc || !pc.remoteDescription) return;
    const queued = iceQueue; iceQueue = [];
    for (const c of queued) { try { await pc.addIceCandidate(c); } catch(e) {} }
  }

  socket.on('call:offer', d => {
    iceQueue = []; pendingOffer = d;
    if (d.premade)         premadeActive  = true;
    if (d.has_mouth_track) _hasMouthTrack = true;
    if (d.mouth_x != null) _pendingMouthX = d.mouth_x;
    if (d.mouth_y != null) _pendingMouthY = d.mouth_y;
    showBanner(d.name, d.type === 'video');
  });

  async function acceptCall() {
    const d = pendingOffer; if (!d) return;
    hideBanner(); setModalIdentity(d.name, false, ''); showModal(d.name);
    if (IS_PF && d.type === 'video' && CALL_VIDEO_URL && !d.premade) {
      $('gcmStatus').textContent = 'Incoming video call...';
      $('gcmIncomingVideoChoice').classList.remove('hidden'); return;
    }
    await answerWithCamera();
  }

  async function answerWithCamera() {
    const d = pendingOffer; if (!d) return;
    $('gcmIncomingVideoChoice').classList.add('hidden');
    $('gcmActiveControls').classList.remove('hidden');
    $('gcmStatus').textContent = 'Connecting...';
    try {
      localStream = await navigator.mediaDevices.getUserMedia({ audio: true, video: d.type === 'video' });
    } catch(e) {
      $('gcmStatus').textContent = 'Microphone/camera access denied';
      socket.emit('call:end', { to: d.from }); setTimeout(cleanup, 1500); return;
    }
    $('gcmLocalVideo').srcObject = localStream;
    if (d.type === 'video') $('gcmLocalVideo').classList.remove('hidden');
    setupPeerConnection(d.from);
    localStream.getTracks().forEach(t => pc.addTrack(t, localStream));
    await pc.setRemoteDescription(d.sdp); await flushIce();
    const ans = await pc.createAnswer(); await pc.setLocalDescription(ans);
    socket.emit('call:answer', { to: d.from, sdp: ans });
  }

  async function answerWithPremade() {
    const d = pendingOffer; if (!d) return;
    $('gcmIncomingVideoChoice').classList.add('hidden');
    $('gcmActiveControls').classList.remove('hidden');
    $('gcmStatus').textContent = 'Connecting...';
    try {
      localStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch(e) {
      $('gcmStatus').textContent = 'Microphone access denied';
      socket.emit('call:end', { to: d.from }); setTimeout(cleanup, 1500); return;
    }
    setupPeerConnection(d.from);
    localStream.getTracks().forEach(t => pc.addTrack(t, localStream));

    const mouthStream = await _startMouthCropStream();
    if (mouthStream) {
      mouthStream.getVideoTracks().forEach(t => { localStream.addTrack(t); pc.addTrack(t, localStream); });
    }

    await pc.setRemoteDescription(d.sdp); await flushIce();
    const ans = await pc.createAnswer(); await pc.setLocalDescription(ans);
    socket.emit('call:answer', {
      to: d.from, sdp: ans,
      premade: true, video_url: CALL_VIDEO_URL,
      has_mouth_track: mouthStream ? true : undefined,
    });
    $('gcmStatus').textContent = 'Connected';
  }

  function declineCall() {
    const d = pendingOffer; hideBanner();
    if (d) socket.emit('call:end', { to: d.from }); cleanup();
  }
  function endCall() {
    if (pendingOffer) socket.emit('call:end', { to: pendingOffer.from }); cleanup();
  }

  socket.on('call:answer', async d => {
    if (!pc) return;
    if (d.has_mouth_track) _hasMouthTrack = true;
    if (d.mouth_x != null) _pendingMouthX = d.mouth_x;
    if (d.mouth_y != null) _pendingMouthY = d.mouth_y;

    if (d.premade && d.video_url) {
      premadeActive = true;
      const rv = $('gcmRemoteVideo');
      rv.srcObject = null; rv.src = d.video_url; rv.loop = true; rv.muted = true;
      rv.style.objectPosition = d.mouth_x != null
        ? `${Math.round(d.mouth_x*100)}% ${Math.round(d.mouth_y*100)}%` : '50% 30%';
      rv.play().catch(() => {});
      rv.classList.remove('hidden');
      rv.onpause    = () => { if (premadeActive) rv.play().catch(() => {}); };
      rv.onclick    = e => { if (premadeActive) e.preventDefault(); };
      rv.ondblclick = e => { if (premadeActive) e.preventDefault(); };
      $('gcmStatus').textContent = 'Connected';
    }
    await pc.setRemoteDescription(d.sdp); await flushIce();
    $('gcmStatus').textContent = 'Connected';
  });

  socket.on('call:ice', async d => {
    if (!d.candidate) return;
    if (pc && pc.remoteDescription) { try { await pc.addIceCandidate(d.candidate); } catch(e) {} }
    else iceQueue.push(d.candidate);
  });
  socket.on('call:end',         () => { hideBanner(); cleanup(); });
  socket.on('call:unavailable', d => {
    const reason = d && d.reason;
    $('gcmStatus').textContent = reason === 'busy'    ? 'Busy on another call'
      : reason === 'offline' ? 'Not available right now' : "Can't place this call";
    setTimeout(cleanup, 2200);
  });

  window.addEventListener('pagehide', () => {
    if (pc && pendingOffer) socket.emit('call:end', { to: pendingOffer.from });
  });

  function cleanup() {
    _stopMouthCropStream(); _stopMouthOverlay(); _stopLipSync();
    _hasMouthTrack = false; _pendingMouthX = null; _pendingMouthY = null;
    if (pc) pc.close(); pc = null;
    if (localStream) localStream.getTracks().forEach(t => t.stop()); localStream = null;
    pendingOffer = null; iceQueue = []; premadeActive = false;
    const rv = $('gcmRemoteVideo');
    if (rv) {
      rv.pause(); rv.src = ''; rv.srcObject = null; rv.loop = false; rv.muted = false;
      rv.style.objectPosition = ''; rv.style.opacity = ''; rv.style.pointerEvents = '';
      rv.classList.add('hidden');
    }
    const lv = $('gcmLocalVideo'); if (lv) lv.classList.add('hidden');
    const ra = $('gcmRemoteAudio'); if (ra) { ra.pause(); ra.srcObject = null; ra.remove(); }
    const ivc = $('gcmIncomingVideoChoice'); if (ivc) ivc.classList.add('hidden');
    const ac  = $('gcmActiveControls');      if (ac)  ac.classList.remove('hidden');
    hideModal(); hideBanner();
  }

  function showBanner(fromName, isVideo) {
    const b = $('globalCallBanner'); if (!b) return;
    $('gcbName').textContent = fromName || 'Incoming call';
    $('gcbSub').textContent  = isVideo ? 'Incoming video call...' : 'Incoming call...';
    b.classList.remove('hidden');
  }
  function hideBanner() { const b = $('globalCallBanner'); if (b) b.classList.add('hidden'); }
  function showModal(name) {
    const m = $('globalCallModal'); if (!m) return;
    $('gcmName').textContent   = name || '';
    $('gcmStatus').textContent = 'Connecting...';
    m.classList.remove('hidden');
  }
  function hideModal() { const m = $('globalCallModal'); if (m) m.classList.add('hidden'); }

  function toggleMute() {
    if (!localStream) return; isMuted = !isMuted;
    localStream.getAudioTracks().forEach(t => t.enabled = !isMuted);
    const icon = $('gcmMuteIcon');
    if (icon) icon.className = isMuted ? 'fa-solid fa-microphone-slash' : 'fa-solid fa-microphone';
  }
  function toggleSpeaker() { const ra = $('gcmRemoteAudio'); if (ra) ra.muted = !ra.muted; }

})();
