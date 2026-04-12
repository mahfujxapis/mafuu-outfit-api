from flask import Flask, request, jsonify, send_file
import requests
from PIL import Image
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor
import os
import traceback

app = Flask(__name__)
executor = ThreadPoolExecutor(max_workers=10)
session = requests.Session()

# --- Configuration ---
API_KEY = "MAFU"             # Expected API key
BACKGROUND_FILENAME = "outfit.png"  # local background image (put this next to app.py)
IMAGE_TIMEOUT = 8                   # seconds for HTTP requests
CANVAS_SIZE = (800, 800)            # final image (width, height) - fixed from 500x500
# BACKGROUND_MODE: 'contain' keeps entire background visible (letterbox),
# 'cover' fills canvas and crops overflow (recommended for your wide image).
BACKGROUND_MODE = 'cover'           # choose 'cover' or 'contain'

def fetch_player_info(uid: str):
    """Fetch player information from the API"""
    if not uid:
        return None
    player_info_url = f"https://mafuuuu-info-api.vercel.app/mafu-info?uid={uid}"
    try:
        resp = session.get(player_info_url, timeout=IMAGE_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"Error fetching player info: {e}")
        return None

def fetch_and_process_image(image_url: str, size: tuple = None):
    """Fetch image from URL and optionally resize"""
    try:
        resp = session.get(image_url, timeout=IMAGE_TIMEOUT)
        resp.raise_for_status()
        img = Image.open(BytesIO(resp.content)).convert("RGBA")
        if size:
            img = img.resize(size, Image.LANCZOS)
        return img
    except Exception as e:
        print(f"Error fetching image from {image_url}: {e}")
        return None

@app.route('/mafu-outfit-image', methods=['GET'])
def outfit_image():
    """Generate outfit combination image for a player"""
    uid = request.args.get('uid')
    key = request.args.get('key')

    # API key validation
    if key != API_KEY:
        return jsonify({'error': 'Invalid or missing API key'}), 401

    if not uid:
        return jsonify({'error': 'Missing uid parameter'}), 400

    # Fetch player data
    player_data = fetch_player_info(uid)
    if player_data is None:
        return jsonify({'error': 'Failed to fetch player info'}), 500

    # Extract outfit IDs - handle different possible JSON structures
    outfit_ids = []
    
    # Try multiple possible paths for outfit data
    if "AccountProfileInfo" in player_data:
        outfit_ids = player_data.get("AccountProfileInfo", {}).get("EquippedOutfit", [])
    elif "profileInfo" in player_data:
        outfit_ids = player_data.get("profileInfo", {}).get("EquippedOutfit", [])
    elif "basicInfo" in player_data:
        outfit_ids = player_data.get("basicInfo", {}).get("EquippedOutfit", [])
    
    # Ensure outfit_ids is a list
    if not outfit_ids:
        outfit_ids = []
    
    # Define required outfit codes and fallbacks
    # Fixed: removed duplicate "211" and organized properly
    required_starts = ["211", "214", "208", "203", "204", "205", "212"]
    fallback_ids = ["211000000", "214000000", "208000000", "203000000", 
                   "204000000", "205000000", "212000000"]

    used_ids = set()

    def fetch_outfit_image(idx, code):
        """Fetch outfit image for a specific code"""
        matched = None
        for oid in outfit_ids:
            try:
                str_oid = str(oid)
            except Exception:
                continue
            if str_oid.startswith(code) and str_oid not in used_ids:
                matched = str_oid
                used_ids.add(str_oid)
                break
        if matched is None:
            matched = fallback_ids[idx]
        image_url = f'https://iconapi.wasmer.app/{matched}'
        return fetch_and_process_image(image_url, size=(150, 150))

    # Submit all fetch tasks
    futures = []
    for idx, code in enumerate(required_starts):
        futures.append(executor.submit(fetch_outfit_image, idx, code))

    # Load local background image
    bg_path = os.path.join(os.path.dirname(__file__), BACKGROUND_FILENAME)
    try:
        background_image = Image.open(bg_path).convert("RGBA")
        print(f"Background loaded: {background_image.size}")
    except FileNotFoundError:
        return jsonify({'error': f'Background image not found: {BACKGROUND_FILENAME}'}), 500
    except Exception as e:
        return jsonify({'error': f'Failed to open background image: {str(e)}'}), 500

    bg_w, bg_h = background_image.size

    # Determine canvas size & scale mode
    if CANVAS_SIZE is None:
        canvas_w, canvas_h = bg_w, bg_h
        scale_x = scale_y = 1.0
        new_w, new_h = bg_w, bg_h
        background_resized = background_image
        offset_x, offset_y = 0, 0
    else:
        canvas_w, canvas_h = CANVAS_SIZE
        if BACKGROUND_MODE == 'contain':
            scale = min(canvas_w / bg_w, canvas_h / bg_h)
        else:  # 'cover'
            scale = max(canvas_w / bg_w, canvas_h / bg_h)
        new_w = max(1, int(bg_w * scale))
        new_h = max(1, int(bg_h * scale))
        background_resized = background_image.resize((new_w, new_h), Image.LANCZOS)

        # center the resized background on canvas
        offset_x = (canvas_w - new_w) // 2
        offset_y = (canvas_h - new_h) // 2
        scale_x = new_w / bg_w
        scale_y = new_h / bg_h

    # Create canvas and paste background
    canvas = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 255))
    canvas.paste(background_resized, (offset_x, offset_y), background_resized)

    # Positions for each outfit piece (adjusted for 800x800 canvas)
    # These coordinates are proportional to the original design
    positions = [
        {'x': 350, 'y': 30, 'height': 150, 'width': 150},   # head
        {'x': 575, 'y': 130, 'height': 150, 'width': 150},  # faceprint
        {'x': 665, 'y': 350, 'height': 150, 'width': 150},  # mask
        {'x': 575, 'y': 550, 'height': 150, 'width': 150},  # top
        {'x': 350, 'y': 654, 'height': 150, 'width': 150},  # bottom
        {'x': 135, 'y': 570, 'height': 150, 'width': 150},  # shoe
        {'x': 135, 'y': 130, 'height': 150, 'width': 150}   # back attachment
    ]

    # Paste each fetched outfit image onto canvas with scaled positions
    for idx, future in enumerate(futures):
        try:
            outfit_img = future.result(timeout=IMAGE_TIMEOUT)
            if not outfit_img:
                print(f"Failed to fetch outfit {idx}")
                continue
            
            pos = positions[idx]
            paste_x = offset_x + int(pos['x'] * scale_x)
            paste_y = offset_y + int(pos['y'] * scale_y)
            paste_w = max(1, int(pos['width'] * scale_x))
            paste_h = max(1, int(pos['height'] * scale_y))

            resized = outfit_img.resize((paste_w, paste_h), Image.LANCZOS)
            canvas.paste(resized, (paste_x, paste_y), resized)
        except Exception as e:
            print(f"Error pasting outfit {idx}: {e}")
            continue

    # Output PNG
    output = BytesIO()
    canvas.save(output, format='PNG')
    output.seek(0)
    return send_file(output, mimetype='image/png')

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({'status': 'healthy', 'message': 'Outfit API is running'}), 200

@app.route('/', methods=['GET'])
def home():
    """Home endpoint with API info"""
    return jsonify({
        'message': '🎨 MAFU Outfit Image Generator API',
        'endpoints': {
            '/mafu-outfit-image': 'GET - Generate outfit image (requires uid and key params)',
            '/health': 'GET - Health check'
        },
        'usage': '/mafu-outfit-image?uid=YOUR_UID&key=MAFU',
        'note': 'Background image should be named "outfit.png" in the same directory',
        'made_by': 'MAFU',
        'telegram': '@mahfuj_offcial_143'
    })

if __name__ == '__main__':
    print("""
    ╔═══════════════════════════════════════╗
    ║   MAFU Outfit Image Generator API     ║
    ║   Running on http://0.0.0.0:5000     ║
    ╚═══════════════════════════════════════╝
    """)
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)

# ╔══════════════════════════════════════════════════════════════╗
# ║                         CREDITS                              ║
# ║                      MADE BY: MAFU                           ║
# ║                   Telegram: @mahfuj_offcial_143              ║
# ║                                                              ║
# ║              DON'T CHANGE OR REMOVE CREDITS                  ║
# ║           RESPECT THE ORIGINAL DEVELOPER'S WORK              ║
# ╚══════════════════════════════════════════════════════════════╝
