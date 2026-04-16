from flask import Flask, request, jsonify, send_file
from PIL import Image, ImageDraw, ImageFont
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor
import requests
import os

app = Flask(__name__)
executor = ThreadPoolExecutor(max_workers=10)
session = requests.Session()

# --- Configuration ---
API_KEY = "MAFU"
BACKGROUND_FILENAME = "outfit.png"  # আপনার নতুন টেমপ্লেট
IMAGE_TIMEOUT = 8
CANVAS_SIZE = (800, 600)  # নতুন সাইজ
ASSET_BASE_URL = "https://free-ff-api-src-5plp.onrender.com/api/v1/image?itemID="

# Slot positions (x1, y1, x2, y2) for outfit items
SLOT_POSITIONS = [
    (50, 100, 150, 200),   # Left 1 (Outfit 1)
    (50, 250, 150, 350),   # Left 2 (Outfit 2)
    (50, 400, 150, 500),   # Left 3 (Outfit 3)
    (650, 100, 750, 200),  # Right 1 (Outfit 4)
    (650, 250, 750, 350),  # Right 2 (Outfit 5)
    (650, 400, 750, 500),  # Right 3 (Outfit 6)
]

# Character center area
CHARACTER_BOX = (250, 80, 550, 520)  # (left, top, right, bottom)

def fetch_player_info(uid: str):
    if not uid:
        return None
    player_info_url = f"https://mafuuuu-info-api.vercel.app/mafu-info?uid={uid}"
    try:
        resp = session.get(player_info_url, timeout=IMAGE_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None

def fetch_and_process_image(image_url: str, size: tuple = None):
    try:
        resp = session.get(image_url, timeout=IMAGE_TIMEOUT)
        resp.raise_for_status()
        img = Image.open(BytesIO(resp.content)).convert("RGBA")
        if size:
            img = img.resize(size, Image.LANCZOS)
        return img
    except Exception:
        return None

def get_character_image(uid: str):
    """Fetch player character image"""
    # Try to get character image from API
    char_url = f"https://free-ff-api-src-5plp.onrender.com/api/v1/character?uid={uid}"
    img = fetch_and_process_image(char_url, (280, 420))
    if img:
        return img
    
    # Fallback: create placeholder
    return None

def create_outfit_image(player_data: dict, uid: str):
    """Create outfit showcase image"""
    
    # Load background template
    if not os.path.exists(BACKGROUND_FILENAME):
        return None
    
    canvas = Image.open(BACKGROUND_FILENAME).convert("RGBA")
    draw = ImageDraw.Draw(canvas)
    
    # Get outfit IDs from player data
    outfit_ids = []
    if 'profileInfo' in player_data:
        profile = player_data['profileInfo']
        outfit_ids = profile.get('clothes', []) or profile.get('EquippedOutfit', [])
    
    # Get player info
    player_name = "Unknown"
    player_level = "?"
    if 'basicInfo' in player_data:
        player_name = player_data['basicInfo'].get('nickname', 'Unknown')
        player_level = str(player_data['basicInfo'].get('level', '?'))
    
    # 1. Place Character in Center
    char_img = get_character_image(uid)
    if char_img:
        # Center the character
        char_x = (CHARACTER_BOX[0] + CHARACTER_BOX[2]) // 2 - char_img.width // 2
        char_y = (CHARACTER_BOX[1] + CHARACTER_BOX[3]) // 2 - char_img.height // 2
        canvas.paste(char_img, (char_x, char_y), char_img)
    
    # 2. Place Outfit Items in Side Slots
    for idx, item_id in enumerate(outfit_ids[:6]):  # Max 6 items
        if idx >= len(SLOT_POSITIONS):
            break
            
        # Fetch outfit item image
        item_url = f"{ASSET_BASE_URL}{item_id}"
        item_img = fetch_and_process_image(item_url)
        
        if item_img:
            # Get slot position
            x1, y1, x2, y2 = SLOT_POSITIONS[idx]
            slot_width = x2 - x1
            slot_height = y2 - y1
            
            # Resize to fit slot (with padding)
            item_img.thumbnail((slot_width - 20, slot_height - 40), Image.LANCZOS)
            
            # Center in slot
            item_x = x1 + (slot_width - item_img.width) // 2
            item_y = y1 + 30 + (slot_height - 30 - item_img.height) // 2
            
            canvas.paste(item_img, (item_x, item_y), item_img)
    
    # 3. Update Player Info Text at Bottom
    try:
        font = ImageFont.truetype("arial.ttf", 20)
    except:
        font = ImageFont.load_default()
    
    info_text = f"Player: {player_name} | Level: {player_level}"
    # Clear area first
    draw.rectangle([0, 530, 800, 600], fill=(20, 20, 30, 255))
    # Draw text
    bbox = draw.textbbox((0, 0), info_text, font=font)
    text_w = bbox[2] - bbox[0]
    text_x = 400 - text_w // 2
    draw.text((text_x, 550), info_text, fill=(255, 255, 255, 255), font=font)
    
    return canvas

@app.route('/mafu-outfit-image', methods=['GET'])
def outfit_image():
    uid = request.args.get('uid')
    key = request.args.get('key')

    if key != API_KEY:
        return jsonify({'error': 'Invalid or missing API key'}), 401
    
    if not uid:
        return jsonify({'error': 'Missing UID parameter'}), 400

    player_data = fetch_player_info(uid)
    
    if not player_data:
        return jsonify({'error': 'Player Not Found', 'message': 'Could not fetch player data'}), 404
    
    if 'error' in player_data:
        return jsonify({
            'error': 'Player Not Found',
            'message': player_data.get('message', 'Player not found')
        }), 404

    try:
        result_image = create_outfit_image(player_data, uid)
        
        if not result_image:
            return jsonify({'error': 'Image generation failed'}), 500
        
        img_io = BytesIO()
        result_image.save(img_io, 'PNG')
        img_io.seek(0)
        
        return send_file(img_io, mimetype='image/png')
        
    except Exception as e:
        return jsonify({'error': 'Image generation failed', 'message': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)