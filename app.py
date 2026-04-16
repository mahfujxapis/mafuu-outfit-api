from flask import Flask, request, jsonify, send_file
from PIL import Image, ImageDraw, ImageFont
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import os
import time
import json
from functools import lru_cache
from datetime import datetime, timedelta

app = Flask(__name__)
executor = ThreadPoolExecutor(max_workers=10)
session = requests.Session()

# --- Configuration ---
API_KEY = "MAFU"
BACKGROUND_FILENAME = "outfit.png"  # আপনার টেমপ্লেট
IMAGE_TIMEOUT = 8
CANVAS_SIZE = (800, 600)
ASSET_BASE_URL = "https://free-ff-api-src-5plp.onrender.com/api/v1/image?itemID="

# Cache configuration
CACHE_DURATION = 300  # 5 minutes cache
player_cache = {}
image_cache = {}

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

# API Endpoints
PLAYER_INFO_API = "https://mafuuuu-info-api.vercel.app/mafu-info"
CHARACTER_API = "https://free-ff-api-src-5plp.onrender.com/api/v1/character"
ALTERNATIVE_INFO_API = "https://freefireapi.com/api/v1/player"  # Alternative API

def get_cached_or_fetch(cache_dict, key, fetch_func, *args, **kwargs):
    """Generic cache handler"""
    if key in cache_dict:
        cached_data, timestamp = cache_dict[key]
        if datetime.now() - timestamp < timedelta(seconds=CACHE_DURATION):
            return cached_data
    
    data = fetch_func(*args, **kwargs)
    if data:
        cache_dict[key] = (data, datetime.now())
    return data

def fetch_player_info(uid: str):
    """Fetch player info with retry and fallback"""
    if not uid:
        return None
    
    # Try primary API
    player_info_url = f"{PLAYER_INFO_API}?uid={uid}"
    
    for attempt in range(3):  # Retry 3 times
        try:
            resp = session.get(player_info_url, timeout=IMAGE_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            
            # Validate data structure
            if data and ('basicInfo' in data or 'profileInfo' in data):
                return data
        except requests.RequestException as e:
            if attempt == 2:  # Last attempt
                print(f"Failed to fetch player info: {e}")
            else:
                time.sleep(1)  # Wait before retry
        except json.JSONDecodeError:
            if attempt == 2:
                print("Invalid JSON response")
            else:
                time.sleep(1)
    
    return None

def fetch_and_process_image(image_url: str, size: tuple = None):
    """Fetch and process image with caching"""
    if not image_url:
        return None
    
    cache_key = f"{image_url}_{size}"
    
    def fetch_image():
        try:
            resp = session.get(image_url, timeout=IMAGE_TIMEOUT)
            resp.raise_for_status()
            img = Image.open(BytesIO(resp.content)).convert("RGBA")
            if size:
                # Maintain aspect ratio
                img.thumbnail(size, Image.LANCZOS)
            return img
        except Exception as e:
            print(f"Image fetch error: {e}")
            return None
    
    return get_cached_or_fetch(image_cache, cache_key, fetch_image)

def create_placeholder_character():
    """Create a placeholder character image"""
    img = Image.new('RGBA', (280, 420), (50, 50, 70, 255))
    draw = ImageDraw.Draw(img)
    
    # Draw simple character silhouette
    # Head
    draw.ellipse([90, 50, 190, 150], fill=(100, 100, 130, 255))
    # Body
    draw.rectangle([100, 150, 180, 300], fill=(80, 80, 110, 255))
    # Arms
    draw.rectangle([60, 160, 100, 250], fill=(80, 80, 110, 255))
    draw.rectangle([180, 160, 220, 250], fill=(80, 80, 110, 255))
    # Legs
    draw.rectangle([110, 300, 145, 400], fill=(70, 70, 100, 255))
    draw.rectangle([155, 300, 190, 400], fill=(70, 70, 100, 255))
    
    return img

def get_character_image(uid: str):
    """Fetch player character image with fallback"""
    # Try primary character API
    char_url = f"{CHARACTER_API}?uid={uid}"
    img = fetch_and_process_image(char_url, (280, 420))
    
    if img:
        return img
    
    # Return placeholder if API fails
    return create_placeholder_character()

def fetch_outfit_items_parallel(outfit_ids):
    """Fetch multiple outfit items in parallel"""
    if not outfit_ids:
        return []
    
    futures = []
    for item_id in outfit_ids[:6]:
        item_url = f"{ASSET_BASE_URL}{item_id}"
        future = executor.submit(fetch_and_process_image, item_url)
        futures.append((item_id, future))
    
    results = []
    for item_id, future in futures:
        try:
            img = future.result(timeout=IMAGE_TIMEOUT)
            if img:
                results.append(img)
            else:
                results.append(None)
        except Exception:
            results.append(None)
    
    return results

def create_outfit_image(player_data: dict, uid: str):
    """Create outfit showcase image with real-time data"""
    
    # Load background template
    if not os.path.exists(BACKGROUND_FILENAME):
        # Create default background if file not found
        canvas = Image.new('RGBA', CANVAS_SIZE, (30, 30, 50, 255))
        draw = ImageDraw.Draw(canvas)
        # Draw some decorative elements
        draw.rectangle([10, 10, 790, 590], outline=(100, 100, 150, 255), width=3)
        draw.rectangle([15, 15, 785, 585], outline=(80, 80, 120, 255), width=1)
    else:
        canvas = Image.open(BACKGROUND_FILENAME).convert("RGBA")
    
    draw = ImageDraw.Draw(canvas)
    
    # Extract player data with better error handling
    player_name = "Unknown Player"
    player_level = "?"
    player_uid = uid
    player_region = "Unknown"
    player_likes = "0"
    
    if player_data:
        # Basic Info
        if 'basicInfo' in player_data:
            basic = player_data['basicInfo']
            player_name = basic.get('nickname', basic.get('name', 'Unknown Player'))
            player_level = str(basic.get('level', '?'))
            player_region = basic.get('region', 'Unknown')
        
        # Profile Info
        if 'profileInfo' in player_data:
            profile = player_data['profileInfo']
            player_likes = str(profile.get('likes', profile.get('Likes', '0')))
        
        # Account Info
        if 'accountInfo' in player_data:
            account = player_data['accountInfo']
            if not player_level or player_level == '?':
                player_level = str(account.get('level', '?'))
    
    # Get outfit IDs
    outfit_ids = []
    if player_data and 'profileInfo' in player_data:
        profile = player_data['profileInfo']
        outfit_ids = profile.get('clothes', []) or profile.get('EquippedOutfit', [])
    
    # 1. Place Character in Center
    char_img = get_character_image(uid)
    if char_img:
        # Center the character
        char_x = (CHARACTER_BOX[0] + CHARACTER_BOX[2]) // 2 - char_img.width // 2
        char_y = (CHARACTER_BOX[1] + CHARACTER_BOX[3]) // 2 - char_img.height // 2
        canvas.paste(char_img, (char_x, char_y), char_img)
    
    # 2. Place Outfit Items in Side Slots (Parallel fetching)
    outfit_images = fetch_outfit_items_parallel(outfit_ids)
    
    for idx, item_img in enumerate(outfit_images):
        if idx >= len(SLOT_POSITIONS):
            break
        
        if item_img:
            # Get slot position
            x1, y1, x2, y2 = SLOT_POSITIONS[idx]
            slot_width = x2 - x1
            slot_height = y2 - y1
            
            # Resize to fit slot with padding
            item_img_copy = item_img.copy()
            item_img_copy.thumbnail((slot_width - 20, slot_height - 40), Image.LANCZOS)
            
            # Draw slot background
            draw.rectangle([x1, y1, x2, y2], fill=(40, 40, 60, 200), outline=(100, 100, 150, 255), width=2)
            
            # Center in slot
            item_x = x1 + (slot_width - item_img_copy.width) // 2
            item_y = y1 + 30 + (slot_height - 30 - item_img_copy.height) // 2
            
            canvas.paste(item_img_copy, (item_x, item_y), item_img_copy)
        else:
            # Draw empty slot placeholder
            x1, y1, x2, y2 = SLOT_POSITIONS[idx]
            draw.rectangle([x1, y1, x2, y2], fill=(30, 30, 50, 150), outline=(80, 80, 120, 255), width=1)
    
    # 3. Enhanced Player Info Section
    try:
        # Try multiple font options
        font_options = ["arial.ttf", "DejaVuSans.ttf", "FreeSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
        font_large = None
        font_small = None
        
        for font_path in font_options:
            try:
                font_large = ImageFont.truetype(font_path, 24)
                font_small = ImageFont.truetype(font_path, 16)
                break
            except:
                continue
        
        if not font_large:
            font_large = ImageFont.load_default()
            font_small = ImageFont.load_default()
    except:
        font_large = ImageFont.load_default()
        font_small = ImageFont.load_default()
    
    # Clear info area
    draw.rectangle([0, 520, 800, 600], fill=(20, 20, 35, 255))
    draw.line([0, 520, 800, 520], fill=(100, 100, 150, 255), width=2)
    
    # Draw enhanced player info
    info_lines = [
        f"Player: {player_name}",
        f"UID: {player_uid} | Level: {player_level}",
        f"Region: {player_region} | Likes: {player_likes}"
    ]
    
    y_offset = 535
    for line in info_lines:
        bbox = draw.textbbox((0, 0), line, font=font_small)
        text_w = bbox[2] - bbox[0]
        text_x = 400 - text_w // 2
        draw.text((text_x, y_offset), line, fill=(255, 255, 255, 255), font=font_small)
        y_offset += 20
    
    # Add timestamp for real-time indication
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    draw.text((10, 580), f"Updated: {timestamp}", fill=(150, 150, 180, 255), font=ImageFont.load_default())
    
    return canvas

@app.route('/mafu-outfit-image', methods=['GET'])
def outfit_image():
    """Main endpoint for outfit image generation"""
    uid = request.args.get('uid')
    key = request.args.get('key')
    force_refresh = request.args.get('refresh', 'false').lower() == 'true'

    # Authentication
    if key != API_KEY:
        return jsonify({'error': 'Invalid or missing API key'}), 401
    
    if not uid:
        return jsonify({'error': 'Missing UID parameter'}), 400
    
    # Validate UID format (basic validation)
    if not uid.isdigit() or len(uid) < 6:
        return jsonify({'error': 'Invalid UID format'}), 400
    
    # Clear cache if force refresh
    if force_refresh:
        if uid in player_cache:
            del player_cache[uid]
        image_cache.clear()

    # Fetch player data with caching
    def fetch_func():
        return fetch_player_info(uid)
    
    player_data = get_cached_or_fetch(player_cache, uid, fetch_func)
    
    if not player_data:
        return jsonify({
            'error': 'Player Not Found',
            'message': 'Could not fetch player data. Please check UID and try again.',
            'uid': uid
        }), 404
    
    if 'error' in player_data:
        return jsonify({
            'error': 'Player Not Found',
            'message': player_data.get('message', 'Player data unavailable'),
            'uid': uid
        }), 404

    try:
        # Generate image
        result_image = create_outfit_image(player_data, uid)
        
        if not result_image:
            return jsonify({'error': 'Image generation failed'}), 500
        
        # Save to BytesIO
        img_io = BytesIO()
        result_image.save(img_io, 'PNG', optimize=True)
        img_io.seek(0)
        
        # Return image with cache headers
        response = send_file(img_io, mimetype='image/png')
        response.headers['Cache-Control'] = f'public, max-age={CACHE_DURATION}'
        response.headers['X-UID'] = uid
        response.headers['X-Generated'] = datetime.now().isoformat()
        
        return response
        
    except Exception as e:
        app.logger.error(f"Image generation error for UID {uid}: {str(e)}")
        return jsonify({
            'error': 'Image generation failed',
            'message': 'Internal server error occurred',
            'details': str(e) if app.debug else None
        }), 500

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'cache_size': {
            'players': len(player_cache),
            'images': len(image_cache)
        }
    })

@app.route('/clear-cache', methods=['POST'])
def clear_cache():
    """Admin endpoint to clear cache"""
    key = request.args.get('key')
    if key != API_KEY:
        return jsonify({'error': 'Invalid API key'}), 401
    
    player_cache.clear()
    image_cache.clear()
    
    return jsonify({
        'status': 'success',
        'message': 'Cache cleared successfully',
        'timestamp': datetime.now().isoformat()
    })

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)