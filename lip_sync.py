import cv2
import numpy as np
from datetime import datetime

try:
    import dlib
    DLIB_AVAILABLE = True
except ImportError:
    DLIB_AVAILABLE = False


class LipSyncProcessor:
    def __init__(self):
        if DLIB_AVAILABLE:
            try:
                self.detector = dlib.get_frontal_face_detector()
                try:
                    self.predictor = dlib.shape_predictor("shape_predictor_68_face_landmarks.dat")
                except Exception:
                    self.predictor = None
            except Exception:
                self.detector = None
                self.predictor = None
        else:
            self.detector = None
            self.predictor = None

        self.mouth_landmarks = list(range(48, 68))   # 68-point model: 48-67 = mouth
        # Jaw + full-face indices for 3-D perspective estimation
        self._face3d_model_pts = np.array([
            (0.0,    0.0,    0.0),    # nose tip          (landmark 30)
            (0.0,   -330.0, -65.0),   # chin              (landmark 8)
            (-225.0, 170.0, -135.0),  # left eye corner   (landmark 36)
            (225.0,  170.0, -135.0),  # right eye corner  (landmark 45)
            (-150.0,-150.0, -125.0),  # left mouth corner (landmark 48)
            (150.0, -150.0, -125.0),  # right mouth corner(landmark 54)
        ], dtype=np.float64)
        self._face3d_landmark_idx = [30, 8, 36, 45, 48, 54]

        self.last_mouth_bbox = None
        self.face_detection_cache = {}

    # ── Mouth-only (fast path, used during live calls) ───────────────────────

    def detect_mouth_only(self, frame, use_cache=True):
        """OPTIMIZED: Detect ONLY mouth region — skip full face processing."""
        try:
            if not DLIB_AVAILABLE or self.detector is None or self.predictor is None:
                return self._fallback_mouth_detection(frame)

            if use_cache and self.last_mouth_bbox is not None:
                x, y, w, h = self.last_mouth_bbox
                if y >= 0 and x >= 0 and (y + h) <= frame.shape[0] and (x + w) <= frame.shape[1]:
                    mouth_region = frame[y:y+h, x:x+w]
                    if mouth_region.size > 0:
                        return mouth_region, (x, y, w, h), True

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = self.detector(gray, 1)
            if len(faces) == 0:
                return None, None, False

            face = faces[0]
            landmarks = self.predictor(gray, face)
            mouth_pts = np.array([[landmarks.part(i).x, landmarks.part(i).y]
                                  for i in self.mouth_landmarks])
            x, y, w, h = cv2.boundingRect(mouth_pts)
            self.last_mouth_bbox = (x, y, w, h)
            mouth_region = frame[y:y+h, x:x+w]
            return mouth_region, (x, y, w, h), True
        except Exception:
            return None, None, False

    def _fallback_mouth_detection(self, frame):
        try:
            h, w = frame.shape[:2]
            mouth_y = int(h * 0.55)
            mouth_h = int(h * 0.2)
            mouth_x = int(w * 0.25)
            mouth_w = int(w * 0.5)
            mouth_y = max(0, min(mouth_y, h - mouth_h))
            mouth_x = max(0, min(mouth_x, w - mouth_w))
            mouth_region = frame[mouth_y:mouth_y+mouth_h, mouth_x:mouth_x+mouth_w]
            if mouth_region.size > 0:
                self.last_mouth_bbox = (mouth_x, mouth_y, mouth_w, mouth_h)
                return mouth_region, (mouth_x, mouth_y, mouth_w, mouth_h), True
            return None, None, False
        except Exception:
            return None, None, False

    # ── Full 3-D face & mouth analysis (offline video processing) ────────────

    def detect_face_and_mouth_3d(self, frame):
        """
        Detect face, compute 3-D head pose, and extract precise mouth data.

        Returns a dict:
          face_bbox   : (x, y, w, h) in pixels
          mouth_bbox  : (x, y, w, h) in pixels
          mouth_center: (cx_norm, cy_norm) — 0-1 fractions of frame size
          mouth_pts   : list of (x, y) landmark points (mouth outline)
          head_pose   : {'roll': °, 'pitch': °, 'yaw': °} in degrees
          rotation_vec: (3,1) ndarray — Rodrigues rotation vector
          translation_vec: (3,1) ndarray
        Returns None if no face found.
        """
        if not DLIB_AVAILABLE or self.detector is None or self.predictor is None:
            return self._fallback_face_3d(frame)

        try:
            h, w = frame.shape[:2]
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = self.detector(gray, 0)
            if len(faces) == 0:
                return None

            face = faces[0]
            landmarks = self.predictor(gray, face)

            # ── Mouth points ──────────────────────────────────────────────
            mouth_pts = [(landmarks.part(i).x, landmarks.part(i).y)
                         for i in self.mouth_landmarks]
            mouth_arr = np.array(mouth_pts)
            mx, my, mw, mh = cv2.boundingRect(mouth_arr)
            # Add 20 % padding so we capture lip context
            pad_x = int(mw * 0.2); pad_y = int(mh * 0.2)
            mx = max(0, mx - pad_x); my = max(0, my - pad_y)
            mw = min(w - mx, mw + 2*pad_x); mh = min(h - my, mh + 2*pad_y)

            cx_norm = (mx + mw / 2) / w
            cy_norm = (my + mh / 2) / h

            # ── 3-D head pose via solvePnP ────────────────────────────────
            image_pts = np.array([
                (landmarks.part(i).x, landmarks.part(i).y)
                for i in self._face3d_landmark_idx
            ], dtype=np.float64)

            focal = w  # reasonable approximation
            cam_matrix = np.array([
                [focal, 0,     w / 2],
                [0,     focal, h / 2],
                [0,     0,     1    ]
            ], dtype=np.float64)
            dist_coeffs = np.zeros((4, 1))

            success, rvec, tvec = cv2.solvePnP(
                self._face3d_model_pts, image_pts,
                cam_matrix, dist_coeffs,
                flags=cv2.SOLVEPNP_ITERATIVE
            )

            if not success:
                rvec = np.zeros((3, 1)); tvec = np.zeros((3, 1))

            # Convert rotation vector to Euler angles (degrees)
            rot_mat, _ = cv2.Rodrigues(rvec)
            sy = np.sqrt(rot_mat[0,0]**2 + rot_mat[1,0]**2)
            if sy > 1e-6:
                pitch = np.degrees(np.arctan2(-rot_mat[2,0], sy))
                yaw   = np.degrees(np.arctan2(rot_mat[2,1], rot_mat[2,2]))
                roll  = np.degrees(np.arctan2(rot_mat[1,0], rot_mat[0,0]))
            else:
                pitch = np.degrees(np.arctan2(-rot_mat[2,0], sy))
                yaw   = 0.0
                roll  = np.degrees(np.arctan2(-rot_mat[1,2], rot_mat[1,1]))

            face_rect = face  # dlib rectangle
            fb = (face_rect.left(), face_rect.top(),
                  face_rect.width(), face_rect.height())

            return {
                'face_bbox':       fb,
                'mouth_bbox':      (mx, my, mw, mh),
                'mouth_center':    (float(cx_norm), float(cy_norm)),
                'mouth_pts':       mouth_pts,
                'head_pose':       {'roll': float(roll), 'pitch': float(pitch), 'yaw': float(yaw)},
                'rotation_vec':    rvec.tolist(),
                'translation_vec': tvec.tolist(),
            }
        except Exception:
            return None

    def _fallback_face_3d(self, frame):
        """Best-effort face/mouth estimate without dlib."""
        h, w = frame.shape[:2]
        # Approximate face as centre 60 % of frame
        fb = (int(w*0.2), int(h*0.1), int(w*0.6), int(h*0.8))
        mx = int(w*0.3); my = int(h*0.6); mw = int(w*0.4); mh = int(h*0.15)
        return {
            'face_bbox':       fb,
            'mouth_bbox':      (mx, my, mw, mh),
            'mouth_center':    (0.5, 0.70),
            'mouth_pts':       [],
            'head_pose':       {'roll': 0.0, 'pitch': 0.0, 'yaw': 0.0},
            'rotation_vec':    [[0],[0],[0]],
            'translation_vec': [[0],[0],[0]],
        }

    # ── Frame-by-frame video analysis (public-figure upload) ─────────────────

    def analyze_video_face_map(self, video_path, sample_rate=5, progress_cb=None):
        """
        Analyse every `sample_rate`-th frame of *video_path* and return:
          - best_mouth_center: (x, y) 0-1 fractions — median across stable frames
          - frame_data: list of per-sampled-frame dicts with face/mouth/pose info
          - face_found: bool
          - summary: human-readable string

        This is the heavy offline pass run after a public figure uploads a video.
        `progress_cb(pct)` is called with 0-100 if provided.
        """
        cap = cv2.VideoCapture(video_path)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
        frame_data = []
        frame_idx = 0
        processed = 0

        while True:
            ret, frame = cap.read()
            if not ret:
                break

            if frame_idx % sample_rate == 0:
                result = self.detect_face_and_mouth_3d(frame)
                if result:
                    result['frame_idx'] = frame_idx
                    frame_data.append(result)
                processed += 1
                if progress_cb:
                    pct = min(99, int(frame_idx / total * 100))
                    progress_cb(pct)

            frame_idx += 1

        cap.release()
        if progress_cb:
            progress_cb(100)

        if not frame_data:
            return {
                'best_mouth_center': None,
                'frame_data': [],
                'face_found': False,
                'summary': 'No face detected in video',
            }

        # ── Pick the most stable frames (low yaw/pitch = face is forward) ──
        stable = [f for f in frame_data
                  if abs(f['head_pose']['yaw']) < 25 and abs(f['head_pose']['pitch']) < 20]
        pool = stable if stable else frame_data

        xs = [f['mouth_center'][0] for f in pool]
        ys = [f['mouth_center'][1] for f in pool]
        best_x = float(np.median(xs))
        best_y = float(np.median(ys))

        return {
            'best_mouth_center': (best_x, best_y),
            'frame_data': frame_data,
            'face_found': True,
            'summary': (
                f"Analysed {len(frame_data)} sampled frames — "
                f"face detected in {len(pool)} stable-pose frames. "
                f"Median mouth centre: ({best_x:.3f}, {best_y:.3f})"
            ),
        }

    # ── Descriptor / matching helpers (live-session lip-sync) ────────────────

    def get_mouth_descriptor_fast(self, mouth_region):
        if mouth_region is None or mouth_region.size == 0:
            return None
        try:
            gray = cv2.cvtColor(mouth_region, cv2.COLOR_BGR2GRAY)
            hist = cv2.calcHist([gray], [0], None, [16], [0, 256])
            hist = cv2.normalize(hist, hist).flatten()
            avg_intensity = np.mean(gray)
            openness = min(avg_intensity / 255.0, 1.0)
            return {
                'histogram': hist,
                'openness': float(openness),
                'intensity': float(avg_intensity),
            }
        except Exception:
            return None

    def extract_video_mouth_only(self, video_path, sample_rate=10):
        cap = cv2.VideoCapture(video_path)
        descriptors_data = []
        frame_count = 0
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            if frame_count % sample_rate == 0:
                mouth_region, bbox, detected = self.detect_mouth_only(frame, use_cache=False)
                if detected and mouth_region is not None:
                    descriptor = self.get_mouth_descriptor_fast(mouth_region)
                    if descriptor:
                        descriptors_data.append({
                            'frame': frame_count,
                            'descriptor': descriptor,
                            'bbox': bbox,
                        })
            frame_count += 1
        cap.release()
        self.last_mouth_bbox = None
        return descriptors_data

    def find_best_matching_frame_fast(self, current_descriptor, video_descriptors):
        if not video_descriptors or current_descriptor is None:
            return 0, None
        min_distance = float('inf')
        best_frame = 0; best_bbox = None
        current_hist = current_descriptor['histogram']
        current_openness = current_descriptor['openness']
        for data in video_descriptors:
            vd = data['descriptor']
            hist_distance = cv2.compareHist(
                current_hist.astype(np.float32),
                vd['histogram'].astype(np.float32),
                cv2.HISTCMP_BHATTACHARYYA,
            )
            openness_distance = abs(current_openness - vd['openness'])
            distance = (hist_distance * 0.7) + (openness_distance * 0.3)
            if distance < min_distance:
                min_distance = distance
                best_frame = data['frame']
                best_bbox = data['bbox']
        return best_frame, best_bbox

    def blend_mouth_only(self, source_mouth, target_frame, target_bbox):
        try:
            if source_mouth is None or source_mouth.size == 0:
                return target_frame
            x, y, w, h = target_bbox
            if y < 0 or x < 0 or (y + h) > target_frame.shape[0] or (x + w) > target_frame.shape[1]:
                return target_frame
            resized_mouth = cv2.resize(source_mouth, (w, h), interpolation=cv2.INTER_LINEAR)
            mask = np.ones((h, w, 3), dtype=np.float32) * 0.8
            mask[:5] *= 0.3; mask[-5:] *= 0.3
            mask[:, :5] *= 0.3; mask[:, -5:] *= 0.3
            target_mouth = target_frame[y:y+h, x:x+w].astype(np.float32)
            blended = target_mouth * (1 - mask) + resized_mouth.astype(np.float32) * mask
            target_frame[y:y+h, x:x+w] = np.uint8(np.clip(blended, 0, 255))
            return target_frame
        except Exception:
            return target_frame
