import os
import urllib.request
import bz2
import shutil

def download_shape_predictor():
    """Download dlib shape predictor for face landmarks"""
    predictor_path = "shape_predictor_68_face_landmarks.dat"
    
    if os.path.exists(predictor_path):
        print("✓ Shape predictor already exists")
        return
    
    url = "http://dlib.net/files/shape_predictor_68_face_landmarks.dat.bz2"
    compressed_path = predictor_path + ".bz2"
    
    print("Downloading facial landmark detector...")
    try:
        urllib.request.urlretrieve(url, compressed_path)
        
        print("Extracting...")
        with bz2.BZ2File(compressed_path) as f_in:
            with open(predictor_path, 'wb') as f_out:
                shutil.copyfileobj(f_in, f_out)
        
        os.remove(compressed_path)
        print("✓ Setup complete!")
    except Exception as e:
        print(f"✗ Error: {e}")
        print("Please download manually from: " + url)

if __name__ == "__main__":
    download_shape_predictor()
