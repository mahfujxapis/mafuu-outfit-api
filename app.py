from flask import Flask, request, jsonify, send_file
from concurrent.futures import ThreadPoolExecutor
import requests
from PIL import Image
from io import BytesIO
import os
import json

app = Flask(__name__)
executor = ThreadPoolExecutor(max_workers=10)
session = requests.Session()

# --- Configuration ---
API_KEY = "MAFU"             # Expected API key
BACKGROUND_FILENAME = "outfit.png"  # local background image (put this next to app.py)
IMAGE_TIMEOUT = 8                   # seconds for HTTP requests
CANVAS_SIZE = (500, 500)            # final image (width, height) or None to use background size
BACKGROUND_MODE = 'cover'           # choose 'cover' or 'contain'

# Outfit item positions on the background
OUTFIT_POSITIONS = [
    {'x': 350, 'y': 30, 'height': 150, 'width': 150},   # 0: head
    {'x': 575, 'y': 130, 'height': 150, 'width': 150},  # 1: faceprint
    {'x': 665, 'y': 350, 'height': 150, 'width': 150},  # 2: mask
    {'x': 575, 'y': 550, 'height': 150, 'width': 150},  # 3: top/outfit
    {'x': 350, 'y': 654, 'height': 150, 'width': 150},  # 4: bottom/pants
    {'x': 135, 'y': 570, 'height': 150, 'width': 150},  # 5: shoes
    {'x': 135, 'y': 130, 'height': 150, 'width': 150}   # 6: extra slot
]

def fetch_player_info(uid: str):
    """Fetch player info from the real API"""
    if not uid:
        return None
    player_info_url = f"https://mafuuuu-info-api.vercel.app/mafu-info?uid={uid}"
    try:
        resp = session.get(player_info_url, timeout=IMAGE_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        print(f"API Response for UID {uid}: {json.dumps(data, indent=2)[:500]}...")  # Debug print
        return data
    except Exception as e:
        print(f"Error fetching player info: {e}")
        return None

def fetch_and_process_image(image_url: str, size: tuple = None):
    """Download and process an image from URL"""
    if not image_url or image_url == "null" or image_url == "None":
        return None
    try:
        resp = session.get(image_url, timeout=IMAGE_TIMEOUT)
        resp.raise_for_status()
        img = Image.open(BytesIO(resp.content)).convert("RGBA")
        if size:
            img = img.resize(size, Image.LANCZOS)
        return img
    except Exception as e:
        print(f"Error fetching image {image_url}: {e}")
        return None

def extract_outfit_urls(player_info: dict):
    """Extract outfit image URLs from the real API response structure"""
    outfit_urls = {}
    
    if not player_info:
        return outfit_urls
    
    # Check different possible data structures
    data = player_info.get('data', player_info)
    
    # Common Free Fire API response structures
    # Structure 1: Direct in data
    outfit_urls['head'] = data.get('avatar') or data.get('Avatar') or data.get('profile_icon')
    outfit_urls['faceprint'] = data.get('faceprint') or data.get('Faceprint')
    outfit_urls['mask'] = data.get('mask') or data.get('Mask')
    outfit_urls['top'] = data.get('outfit') or data.get('Outfit') or data.get('top') or data.get('Top')
    outfit_urls['bottom'] = data.get('pants') or data.get('Pants') or data.get('bottom') or data.get('Bottom')
    outfit_urls['shoes'] = data.get('shoes') or data.get('Shoes') or data.get('shoe') or data.get('Shoe')
    
    # Structure 2: Nested in equipment/items
    equipment = data.get('equipment', {}) or data.get('items', {}) or data.get('inventory', {})
    if equipment:
        outfit_urls['head'] = outfit_urls['head'] or equipment.get('head') or equipment.get('avatar')
        outfit_urls['faceprint'] = outfit_urls['faceprint'] or equipment.get('faceprint')
        outfit_urls['mask'] = outfit_urls['mask'] or equipment.get('mask')
        outfit_urls['top'] = outfit_urls['top'] or equipment.get('outfit') or equipment.get('top')
        outfit_urls['bottom'] = outfit_urls['bottom'] or equipment.get('pants') or equipment.get('bottom')
        outfit_urls['shoes'] = outfit_urls['shoes'] or equipment.get('shoes')
    
    # Structure 3: With icon/image URLs
    for key in ['head', 'faceprint', 'mask', 'top', 'bottom', 'shoes']:
        # Try with _icon, _image, _url suffixes
        if not outfit_urls.get(key):
            outfit_urls[key] = data.get(f'{key}_icon') or data.get(f'{key}_image') or data.get(f'{key}_url')
    
    # Remove None values
    outfit_urls = {k: v for k, v in outfit_urls.items() if v}
    
    print(f"Extracted URLs: {outfit_urls}")
    return outfit_urls

def fetch_outfit_items(player_info: dict):
    """Fetch all outfit item images from player info"""
    outfit_images = []
    
    # Extract URLs from the real data structure
    outfit_urls = extract_outfit_urls(player_info)
    
    # Map outfit types to positions
    outfit_mapping = [
        ('head', 0),
        ('faceprint', 1),
        ('mask', 2),
        ('top', 3),
        ('bottom', 4),
        ('shoes', 5)
    ]
    
    for outfit_type, position_index in outfit_mapping:
        url = outfit_urls.get(outfit_type)
        if url:
            pos = OUTFIT_POSITIONS[position_index]
            size = (pos['width'], pos['height'])
            img = fetch_and_process_image(url, size)
            if img:
                outfit_images.append({
                    'image': img,
                    'position': pos,
                    'type': outfit_type
                })
                print(f"Loaded {outfit_type} from {url}")
    
    return outfit_images

def create_outfit_image(outfit_items, background_img, canvas_size=CANVAS_SIZE):
    """Combine all outfit items with background"""
    try:
        # Create canvas
        canvas = Image.new("RGBA", canvas_size, (255, 255, 255, 0))
        
        # Process background
        if BACKGROUND_MODE == 'cover':
            # Calculate aspect ratios
            bg_ratio = background_img.width / background_img.height
            canvas_ratio = canvas_size[0] / canvas_size[1]
            
            if bg_ratio > canvas_ratio:
                # Background is wider - fit to height
                new_height = canvas_size[1]
                new_width = int(new_height * bg_ratio)
            else:
                # Background is taller - fit to width
                new_width = canvas_size[0]
                new_height = int(new_width / bg_ratio)
            
            background_img = background_img.resize((new_width, new_height), Image.LANCZOS)
            
            # Center crop
            left = (new_width - canvas_size[0]) // 2
            top = (new_height - canvas_size[1]) // 2
            background_img = background_img.crop((left, top, left + canvas_size[0], top + canvas_size[1]))
        else:  # 'contain' mode
            background_img.thumbnail(canvas_size, Image.LANCZOS)
            # Create letterbox
            temp_canvas = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
            x = (canvas_size[0] - background_img.width) // 2
            y = (canvas_size[1] - background_img.height) // 2
            temp_canvas.paste(background_img, (x, y))
            background_img = temp_canvas
        
        # Paste background
        canvas.paste(background_img, (0, 0))
        
        # Paste all outfit items at their positions
        for item in outfit_items:
            x = item['position']['x']
            y = item['position']['y']
            img = item['image']
            
            # Ensure image fits within canvas
            if x + img.width <= canvas_size[0] and y + img.height <= canvas_size[1]:
                canvas.paste(img, (x, y), img)
                print(f"Pasted {item['type']} at ({x}, {y})")
        
        return canvas
    except Exception as e:
        print(f"Error creating outfit image: {e}")
        import traceback
        traceback.print_exc()
        return None

@app.route('/mafu-outfit-image', methods=['GET'])
def outfit_image():
    uid = request.args.get('uid')
    key = request.args.get('key')

    if key != API_KEY:
        return jsonify({'error': 'Invalid or missing API key'}), 401
    
    if not uid:
        return jsonify({'error': 'Missing UID parameter'}), 400
    
    # Fetch player info
    player_info = fetch_player_info(uid)
    if not player_info:
        return jsonify({'error': 'Failed to fetch player information'}), 404
    
    # Fetch all outfit items
    outfit_items = fetch_outfit_items(player_info)
    if not outfit_items:
        # Debug: return the actual API response to see what's available
        return jsonify({
            'error': 'No outfit items found for this player',
            'debug_data': player_info
        }), 404
    
    # Load background image
    try:
        if os.path.exists(BACKGROUND_FILENAME):
            background_img = Image.open(BACKGROUND_FILENAME).convert("RGBA")
        else:
            # Create a default background if file doesn't exist
            background_img = Image.new("RGBA", CANVAS_SIZE, (30, 30, 50, 255))
            print(f"Warning: {BACKGROUND_FILENAME} not found, using default background")
    except Exception as e:
        return jsonify({'error': f'Failed to load background: {str(e)}'}), 500
    
    # Create final outfit image
    final_image = create_outfit_image(outfit_items, background_img)
    if not final_image:
        return jsonify({'error': 'Failed to create outfit image'}), 500
    
    # Save to BytesIO and send
    img_io = BytesIO()
    final_image.save(img_io, 'PNG')
    img_io.seek(0)
    
    return send_file(img_io, mimetype='image/png')

@app.route('/mafu-outfit-json', methods=['GET'])
def outfit_json():
    """Return outfit data as JSON with image URLs"""
    uid = request.args.get('uid')
    key = request.args.get('key')

    if key != API_KEY:
        return jsonify({'error': 'Invalid or missing API key'}), 401
    
    if not uid:
        return jsonify({'error': 'Missing UID parameter'}), 400
    
    # Fetch player info
    player_info = fetch_player_info(uid)
    if not player_info:
        return jsonify({'error': 'Failed to fetch player information'}), 404
    
    # Extract outfit URLs
    outfit_urls = extract_outfit_urls(player_info)
    
    # Format response
    outfit_data = {
        'uid': uid,
        'outfit_items': outfit_urls,
        'positions': OUTFIT_POSITIONS[:6],
        'raw_data': player_info  # Include raw data for debugging
    }
    
    return jsonify(outfit_data)

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({
        'status': 'healthy',
        'positions_available': len(OUTFIT_POSITIONS),
        'background_exists': os.path.exists(BACKGROUND_FILENAME)
    }), 200

@app.route('/test-uid', methods=['GET'])
def test_uid():
    """Test endpoint to see what data the API returns for a UID"""
    uid = request.args.get('uid', '123456')
    player_info = fetch_player_info(uid)
    return jsonify({
        'uid': uid,
        'api_response': player_info
    })

if __name__ == '__main__':
    print(f"Starting Flask server...")
    print(f"Background file: {BACKGROUND_FILENAME}")
    print(f"Background exists: {os.path.exists(BACKGROUND_FILENAME)}")
    print(f"Canvas size: {CANVAS_SIZE}")
    print(f"Positions configured: {len(OUTFIT_POSITIONS)}")
    app.run(host='0.0.0.0', port=5000, debug=True)