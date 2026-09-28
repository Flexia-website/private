import cv2
import numpy as np
from lip_sync import LipSyncProcessor
import os

class LipSyncVideoGenerator:
    """Generate output video with synced lip movements"""
    
    def __init__(self, upload_folder="static/uploads"):
        self.processor = LipSyncProcessor()
        self.upload_folder = upload_folder
    
    def process_video_with_realtime_data(self, video_path, camera_frames, output_path):
        """
        Blend camera mouth movements onto video using real-time camera data
        
        Args:
            video_path: Path to original video
            camera_frames: List of captured camera frames with landmarks
            output_path: Path to save output video
        """
        cap = cv2.VideoCapture(video_path)
        fps = cap.get(cv2.CAP_PROP_FPS)
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        out = cv2.VideoWriter(output_path, fourcc, fps, (width, height))
        
        frame_idx = 0
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            
            if frame_idx < len(camera_frames):
                camera_data = camera_frames[frame_idx]
                frame = self.apply_mouth_sync(frame, camera_data)
            
            out.write(frame)
            frame_idx += 1
        
        cap.release()
        out.release()
        return output_path
    
    def apply_mouth_sync(self, video_frame, camera_data):
        """Apply camera mouth to video frame"""
        try:
            # Get mouth region from camera frame
            camera_frame = camera_data.get('frame')
            if camera_frame is None:
                return video_frame
            
            # Extract mouth from camera
            mouth_region, camera_bbox, detected = self.processor.detect_mouth_only(camera_frame, use_cache=False)
            if not detected or mouth_region is None:
                return video_frame
            
            # Get mouth region from video frame
            video_mouth, video_bbox, video_detected = self.processor.detect_mouth_only(video_frame, use_cache=False)
            if not video_detected or video_bbox is None:
                return video_frame
            
            # Blend camera mouth onto video
            blended_frame = self.processor.blend_mouth_only(mouth_region, video_frame, video_bbox)
            return blended_frame
        except:
            return video_frame
    
    def extract_video_preview_frame(self, video_path, frame_number=0):
        """Extract a preview thumbnail from video"""
        cap = cv2.VideoCapture(video_path)
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_number)
        ret, frame = cap.read()
        cap.release()
        
        if ret:
            frame = cv2.resize(frame, (320, 180))
            return frame
        return None
    
    def save_preview(self, video_path, output_path):
        """Save video preview image"""
        frame = self.extract_video_preview_frame(video_path)
        if frame is not None:
            cv2.imwrite(output_path, frame)
            return True
        return False
    
    def validate_video(self, video_path):
        """Check if video is valid and has faces"""
        try:
            cap = cv2.VideoCapture(video_path)
            if not cap.isOpened():
                return False, "Cannot open video file"
            
            frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            if frame_count < 30:
                return False, "Video too short (minimum 30 frames)"
            
            sample_frames = [0, frame_count // 2, frame_count - 1]
            faces_detected = 0
            
            for frame_idx in sample_frames:
                cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
                ret, frame = cap.read()
                if ret:
                    mouth_region, bbox, detected = self.processor.detect_mouth_only(frame, use_cache=False)
                    if detected:
                        faces_detected += 1
            
            cap.release()
            
            if faces_detected == 0:
                return False, "No faces detected in video"
            
            return True, f"Valid video ({frame_count} frames, {faces_detected} faces detected)"
        except Exception as e:
            return False, f"Error validating: {str(e)}"
    
    def extract_key_frames(self, video_path, num_frames=10):
        """Extract key frames from video for preview"""
        cap = cv2.VideoCapture(video_path)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        
        key_frames = []
        indices = np.linspace(0, total_frames - 1, num_frames, dtype=int)
        
        for idx in indices:
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ret, frame = cap.read()
            if ret:
                frame = cv2.resize(frame, (160, 90))
                key_frames.append(frame)
        
        cap.release()
        return key_frames
    
    def create_thumbnail_grid(self, key_frames, output_path):
        """Create a grid of thumbnail frames"""
        rows = 2
        cols = 5
        
        if len(key_frames) < rows * cols:
            rows = max(1, len(key_frames) // cols)
        
        h, w = key_frames[0].shape[:2]
        grid = np.zeros((h * rows, w * cols, 3), dtype=np.uint8)
        
        for i, frame in enumerate(key_frames[:rows * cols]):
            r = i // cols
            c = i % cols
            grid[r*h:(r+1)*h, c*w:(c+1)*w] = frame
        
        cv2.imwrite(output_path, grid)
        return output_path

class SessionRecorder:
    """Record and manage lip-sync session data"""
    
    def __init__(self):
        self.frames = []
        self.timestamps = []
        self.mouth_data = []
    
    def add_frame(self, frame_data, timestamp):
        """Add a frame to recording"""
        self.frames.append(frame_data)
        self.timestamps.append(timestamp)
    
    def add_mouth_data(self, mouth_descriptor):
        """Add mouth detection data"""
        self.mouth_data.append(mouth_descriptor)
    
    def get_session_duration(self):
        """Calculate session duration in seconds"""
        if len(self.timestamps) < 2:
            return 0
        return self.timestamps[-1] - self.timestamps[0]
    
    def get_face_detection_rate(self):
        """Calculate percentage of frames with detected faces"""
        if not self.mouth_data:
            return 0
        detected = sum(1 for d in self.mouth_data if d is not None)
        return (detected / len(self.mouth_data)) * 100
    
    def get_session_stats(self):
        """Get comprehensive session statistics"""
        return {
            'total_frames': len(self.frames),
            'duration_seconds': self.get_session_duration(),
            'face_detection_rate': self.get_face_detection_rate(),
            'average_mouth_openness': self._calc_avg_openness(),
            'frames_with_face': sum(1 for d in self.mouth_data if d is not None)
        }
    
    def _calc_avg_openness(self):
        """Calculate average mouth openness"""
        openness_values = [
            d['openness'] for d in self.mouth_data 
            if d and 'openness' in d
        ]
        if not openness_values:
            return 0
        return sum(openness_values) / len(openness_values)
    
    def export_session_data(self, filepath):
        """Export session data to JSON"""
        import json
        data = {
            'stats': self.get_session_stats(),
            'timestamps': self.timestamps,
            'mouth_data': self.mouth_data
        }
        with open(filepath, 'w') as f:
            json.dump(data, f, indent=2)
        return filepath
