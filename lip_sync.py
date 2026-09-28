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
                # Try to load shape predictor if available
                try:
                    self.predictor = dlib.shape_predictor("shape_predictor_68_face_landmarks.dat")
                except:
                    self.predictor = None
            except:
                self.detector = None
                self.predictor = None
        else:
            self.detector = None
            self.predictor = None
        self.mouth_landmarks = list(range(48, 68))
        self.last_mouth_bbox = None
        self.face_detection_cache = {}
    
    def detect_mouth_only(self, frame, use_cache=True):
        """OPTIMIZED: Detect ONLY mouth region - skip full face processing"""
        try:
            if not DLIB_AVAILABLE or self.detector is None or self.predictor is None:
                # Fallback: Use simple region-based detection
                return self._fallback_mouth_detection(frame)
            
            # Use cached bbox if available (faster)
            if use_cache and self.last_mouth_bbox is not None:
                x, y, w, h = self.last_mouth_bbox
                if y >= 0 and x >= 0 and (y + h) <= frame.shape[0] and (x + w) <= frame.shape[1]:
                    mouth_region = frame[y:y+h, x:x+w]
                    if mouth_region.size > 0:
                        return mouth_region, (x, y, w, h), True
            
            # Full face detection only when cache missed
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = self.detector(gray, 1)  # 1 = skip some frames
            
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
            
        except:
            return None, None, False
    
    def _fallback_mouth_detection(self, frame):
        """Fallback mouth detection using simple region analysis"""
        try:
            h, w = frame.shape[:2]
            # Mouth is typically in lower third of face (roughly face center)
            # Estimate mouth region as center-lower area
            mouth_y = int(h * 0.55)  # Lower portion of face
            mouth_h = int(h * 0.2)   # Mouth height
            mouth_x = int(w * 0.25)  # Left side start
            mouth_w = int(w * 0.5)   # Mouth width (roughly half face)
            
            # Ensure bounds are valid
            mouth_y = max(0, min(mouth_y, h - mouth_h))
            mouth_x = max(0, min(mouth_x, w - mouth_w))
            
            mouth_region = frame[mouth_y:mouth_y+mouth_h, mouth_x:mouth_x+mouth_w]
            if mouth_region.size > 0:
                self.last_mouth_bbox = (mouth_x, mouth_y, mouth_w, mouth_h)
                return mouth_region, (mouth_x, mouth_y, mouth_w, mouth_h), True
            return None, None, False
        except:
            return None, None, False
    
    def get_mouth_descriptor_fast(self, mouth_region):
        """OPTIMIZED: Ultra-fast mouth descriptor from region only"""
        if mouth_region is None or mouth_region.size == 0:
            return None
        
        try:
            # Convert to grayscale for faster processing
            gray = cv2.cvtColor(mouth_region, cv2.COLOR_BGR2GRAY)
            
            # Simple histogram-based descriptor (fast)
            hist = cv2.calcHist([gray], [0], None, [16], [0, 256])
            hist = cv2.normalize(hist, hist).flatten()
            
            # Calculate simple openness from intensity
            avg_intensity = np.mean(gray)
            openness = min(avg_intensity / 255.0, 1.0)
            
            return {
                'histogram': hist,
                'openness': float(openness),
                'intensity': float(avg_intensity)
            }
        except:
            return None
    
    def extract_video_mouth_only(self, video_path, sample_rate=10):
        """OPTIMIZED: Extract ONLY mouth descriptors from video - skip full processing"""
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
                            'bbox': bbox
                        })
            frame_count += 1
        
        cap.release()
        self.last_mouth_bbox = None
        return descriptors_data
    
    def find_best_matching_frame_fast(self, current_descriptor, video_descriptors):
        """OPTIMIZED: Fast frame matching using histogram comparison"""
        if not video_descriptors or current_descriptor is None:
            return 0, None
        
        min_distance = float('inf')
        best_frame = 0
        best_bbox = None
        
        current_hist = current_descriptor['histogram']
        current_openness = current_descriptor['openness']
        
        for data in video_descriptors:
            vd = data['descriptor']
            
            # Compare histograms (very fast)
            hist_distance = cv2.compareHist(
                current_hist.astype(np.float32),
                vd['histogram'].astype(np.float32),
                cv2.HISTCMP_BHATTACHARYYA
            )
            
            # Compare openness
            openness_distance = abs(current_openness - vd['openness'])
            
            # Combined distance (histogram is 70%, openness is 30%)
            distance = (hist_distance * 0.7) + (openness_distance * 0.3)
            
            if distance < min_distance:
                min_distance = distance
                best_frame = data['frame']
                best_bbox = data['bbox']
        
        return best_frame, best_bbox
    
    def blend_mouth_only(self, source_mouth, target_frame, target_bbox):
        """OPTIMIZED: Direct mouth blending - no extra processing"""
        try:
            if source_mouth is None or source_mouth.size == 0:
                return target_frame
            
            x, y, w, h = target_bbox
            
            # Skip if bbox out of bounds
            if y < 0 or x < 0 or (y + h) > target_frame.shape[0] or (x + w) > target_frame.shape[1]:
                return target_frame
            
            # Resize source mouth to target bbox size
            resized_mouth = cv2.resize(source_mouth, (w, h), interpolation=cv2.INTER_LINEAR)
            
            # Create simple blend mask (feather edges)
            mask = np.ones((h, w, 3), dtype=np.float32) * 0.8
            # Feather edges with Gaussian
            mask[:5] *= 0.3
            mask[-5:] *= 0.3
            mask[:, :5] *= 0.3
            mask[:, -5:] *= 0.3
            
            # Direct alpha blend (fastest)
            target_mouth = target_frame[y:y+h, x:x+w].astype(np.float32)
            blended = (target_mouth * (1 - mask) + resized_mouth.astype(np.float32) * mask)
            
            target_frame[y:y+h, x:x+w] = np.uint8(np.clip(blended, 0, 255))
            return target_frame
            
        except:
            return target_frame
