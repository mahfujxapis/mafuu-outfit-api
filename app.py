from flask import Flask, request, jsonify, send_file
from PIL import Image, ImageDraw, ImageFont
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import os
import time
import json
from datetime import datetime, timedelta

app = Flask(__name__)
executor = ThreadPoolExecutor(max_workers=10)
session = requests.Session()

# --- Configuration ---
API_KEY = "MAFU"
BACKGROUND_FILENAME = "outfit.png"
IMAGE_TIMEOUT = 10
CANVAS_SIZE = (800, 600)
ASSET_BASE_URL = "https://free-ff-api-src-5plp.onrender.com/api/v1/image?itemID="

# Cache configuration
CACHE_DURATION = 300  # 5 minutes
player_cache = {}
image_cache = {}

# Slot positions (x1, y1, x2, y2) for outfit items
SLOT_POSITIONS = [
    (50, 100, 150, 200),   # Left 1
    (50, 250, 150, 350),   # Left 2
    (50, 400, 150, 500),   # Left 3
    (650, 100, 750, 200),  # Right 1
    (650, 250, 750, 350),  # Right 2
    (650, 400, 750, 500),  # Right 3
]

# Character center area
CHARACTER_BOX = (250, 80, 550, 520)

# API Endpoints
PLAYER_INFO_API = "https://mafuuuu-info-api.vercel.app/mafu-info"
CHARACTER_API = "https://free-ff-api-src-5plp.onrender.com/api/v1/character"
FALLBACK_PLAYER_API = "https://api.garena.com/api/v1/player/profile"  # Fallback

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

def extract_player_data(raw_data):
    """Extract player data from API response with multiple fallbacks"""
    player_info = {
        'name': 'Unknown Player',
        'level': '?',
        'uid': '',
        'region': 'Unknown',
        'likes': '0',
        'avatar_url': None,
        'outfit_ids': [],
        'banner_url': None
    }
    
    if not raw_data:
        return player_info
    
    # Debug: Print raw data structure (remove in production)
    print(f"Raw data keys: {raw_data.keys() if isinstance(raw_data, dict) else 'Not a dict'}")
    
    # Try multiple data structures
    # Structure 1: Direct player data
    if isinstance(raw_data, dict):
        # Basic Info - Multiple possible paths
        if 'basicInfo' in raw_data:
            basic = raw_data['basicInfo']
            player_info['name'] = basic.get('nickname') or basic.get('name') or basic.get('playerName') or 'Unknown Player'
            player_info['level'] = str(basic.get('level') or basic.get('playerLevel') or '?')
            player_info['region'] = basic.get('region') or basic.get('country') or 'Unknown'
            player_info['avatar_url'] = basic.get('avatar') or basic.get('avatarUrl') or basic.get('iconUrl')
        elif 'player' in raw_data:
            player = raw_data['player']
            player_info['name'] = player.get('nickname') or player.get('name') or 'Unknown Player'
            player_info['level'] = str(player.get('level') or '?')
        elif 'nickname' in raw_data:
            player_info['name'] = raw_data.get('nickname', 'Unknown Player')
            player_info['level'] = str(raw_data.get('level', '?'))
        
        # Profile Info
        if 'profileInfo' in raw_data:
            profile = raw_data['profileInfo']
            player_info['likes'] = str(profile.get('likes') or profile.get('Likes') or '0')
            player_info['outfit_ids'] = profile.get('clothes') or profile.get('EquippedOutfit') or []
            player_info['banner_url'] = profile.get('banner') or profile.get('bannerUrl')
        elif 'profile' in raw_data:
            profile = raw_data['profile']
            player_info['likes'] = str(profile.get('likes', '0'))
            player_info['outfit_ids'] = profile.get('equipped', [])
        
        # Account Info
        if 'accountInfo' in raw_data:
            account = raw_data['accountInfo']
            if player_info['level'] == '?':
                player_info['level'] = str(account.get('level', '?'))
            player_info['uid'] = str(account.get('uid', ''))
        elif 'account' in raw_data:
            account = raw_data['account']
            if player_info['level'] == '?':
                player_info['level'] = str(account.get('level', '?'))
            player_info['uid'] = str(account.get('uid', ''))
        
        # Direct fields
        if not player_info['name'] or player_info['name'] == 'Unknown Player':
            player_info['name'] = raw_data.get('nickname') or raw_data.get('playerName') or raw_data.get('name') or 'Unknown Player'
        if player_info['level'] == '?':
            player_info['level'] = str(raw_data.get('level') or raw_data.get('playerLevel') or '?')
        if not player_info['outfit_ids']:
            player_info['outfit_ids'] = raw_data.get('outfit') or raw_data.get('equippedOutfit') or []
    
    return player_info

def fetch_player_info(uid: str):
    """Fetch player info with retry and better error handling"""
    if not uid:
        return None
    
    player_info_url = f"{PLAYER_INFO_API}?uid={uid}"
    
    for attempt in range(3):
        try:
            resp = session.get(player_info_url, timeout=IMAGE_TIMEOUT, 
                             headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'})
            resp.raise_for_status()
            
            # Try to parse JSON
            try:
                data = resp.json()
            except json.JSONDecodeError:
                print(f"Attempt {attempt + 1}: Invalid JSON response")
                continue
            
            # Extract and validate data
            player_data = extract_player_data(data)
            
            # Check if we got meaningful data
            if player_data['name'] != 'Unknown Player' or player_data['level'] != '?':
                return player_data
            
            print(f"Attempt {attempt + 1}: Incomplete data received")
            
        except requests.RequestException as e:
            print(f"Attempt {attempt + 1}: Request failed - {e}")
            if attempt < 2:
                time.sleep(1)
    
    # Return minimal data if all attempts fail
    return {
        'name': f'Player_{uid[:6]}',
        'level': '?',
        'uid': uid,
        'region': 'Unknown',
        'likes': '0',
        'avatar_url': None,
        'outfit_ids': [],
        'banner_url': None
    }

def fetch_and_process_image(image_url: str, size: tuple = None):
    """Fetch and process image with caching"""
    if not image_url:
        return None
    
    cache_key = f"{image_url}_{size}"
    
    def fetch_image():
        try:
            headers = {'User-Agent': 'Mozilla/5.0', 'Accept': 'image/*'}
            resp = session.get(image_url, timeout=IMAGE_TIMEOUT, headers=headers)
            resp.raise_for_status()
            img = Image.open(BytesIO(resp.content)).convert("RGBA")
            if size:
                img.thumbnail(size, Image.LANCZOS)
            return img
        except Exception as e:
            print(f"Image fetch error for {image_url}: {e}")
            return None
    
    return get_cached_or_fetch(image_cache, cache_key, fetch_image)

def create_character_placeholder():
    """Create a stylish placeholder character"""
    img = Image.new('RGBA', (280, 420), (45, 45, 65, 255))
    draw = ImageDraw.Draw(img)
    
    # Character silhouette with gradient effect
    # Head
    draw.ellipse([90, 50, 190, 150], fill=(120, 120, 150, 255), outline=(180, 180, 200, 255), width=2)
    # Body
    draw.rectangle([100, 150, 180, 300], fill=(100, 100, 130, 255), outline=(160, 160, 180, 255), width=2)
    # Arms
    draw.rectangle([60, 160, 100, 250], fill=(100, 100, 130, 255), outline=(160, 160, 180, 255), width=2)
    draw.rectangle([180, 160, 220, 250], fill=(100, 100, 130, 255), outline=(160, 160, 180, 255), width=2)
    # Legs
    draw.rectangle([110, 300, 145, 400], fill=(90, 90, 120, 255), outline=(150, 150, 170, 255), width=2)
    draw.rectangle([155, 300, 190, 400], fill=(90, 90, 120, 255), outline=(150, 150, 170, 255), width=2)
    
    # Add text
    draw.text((140, 210), "?", fill=(255, 255, 255, 255), anchor="mm")
    
    return img

def get_character_image(uid: str, avatar_url: str = None):
    """Fetch character image with multiple sources"""
    # Try avatar URL first (usually profile picture)
    if avatar_url:
        img = fetch_and_process_image(avatar_url, (280, 420))
        if img:
            return img
    
    # Try character API
    char_url = f"{CHARACTER_API}?uid={uid}"
    img = fetch_and_process_image(char_url, (280, 420))
    if img:
        return img
    
    # Try alternative character API formats
    alt_urls = [
        f"https://freefiremobile-a.akamaihd.net/common/avatar/{uid}.png",
        f"https://ff.garena.com/api/avatar?uid={uid}"
    ]
    
    for url in alt_urls:
        img = fetch_and_process_image(url, (280, 420))
        if img:
            return img
    
    # Return stylish placeholder
    return create_character_placeholder()

def fetch_outfit_items_parallel(outfit_ids):
    """Fetch multiple outfit items in parallel"""
    if not outfit_ids:
        return []
    
    futures = []
    for item_id in outfit_ids[:6]:
        if item_id:
            item_url = f"{ASSET_BASE_URL}{item_id}"
            future = executor.submit(fetch_and_process_image, item_url)
            futures.append((item_id, future))
        else:
            futures.append((None, None))
    
    results = []
    for item_id, future in futures:
        if future:
            try:
                img = future.result(timeout=IMAGE_TIMEOUT)
                results.append(img)
            except Exception as e:
                print(f"Failed to fetch outfit item {item_id}: {e}")
                results.append(None)
        else:
            results.append(None)
    
    return results

def create_outfit_image(player_data: dict, uid: str):
    """Create outfit showcase image with real data"""
    
    # Load or create background
    if os.path.exists(BACKGROUND_FILENAME):
        canvas = Image.open(BACKGROUND_FILENAME).convert("RGBA")
        # Resize if needed
        if canvas.size != CANVAS_SIZE:
            canvas = canvas.resize(CANVAS_SIZE, Image.LANCZOS)
    else:
        # Create gradient background
        canvas = Image.new('RGBA', CANVAS_SIZE, (25, 25, 40, 255))
        draw = ImageDraw.Draw(canvas)
        # Decorative borders
        draw.rectangle([5, 5, 795, 595], outline=(120, 120, 160, 255), width=3)
        draw.rectangle([10, 10, 790, 590], outline=(80, 80, 120, 255), width=1)
        # Title area
        draw.rectangle([200, 20, 600, 60], fill=(40, 40, 60, 200), outline=(100, 100, 150, 255), width=2)
    
    draw = ImageDraw.Draw(canvas)
    
    # Extract player info
    player_name = player_data.get('name', 'Unknown Player')
    player_level = player_data.get('level', '?')
    player_region = player_data.get('region', 'Unknown')
    player_likes = player_data.get('likes', '0')
    avatar_url = player_data.get('avatar_url')
    outfit_ids = player_data.get('outfit_ids', [])
    
    # 1. Place Character
    char_img = get_character_image(uid, avatar_url)
    if char_img:
        char_x = (CHARACTER_BOX[0] + CHARACTER_BOX[2]) // 2 - char_img.width // 2
        char_y = (CHARACTER_BOX[1] + CHARACTER_BOX[3]) // 2 - char_img.height // 2
        canvas.paste(char_img, (char_x, char_y), char_img)
    
    # 2. Place Outfit Items
    if outfit_ids:
        outfit_images = fetch_outfit_items_parallel(outfit_ids)
        
        for idx, item_img in enumerate(outfit_images):
            if idx >= len(SLOT_POSITIONS):
                break
            
            x1, y1, x2, y2 = SLOT_POSITIONS[idx]
            slot_width = x2 - x1
            slot_height = y2 - y1
            
            # Draw slot background
            draw.rectangle([x1, y1, x2, y2], fill=(35, 35, 55, 200), outline=(90, 90, 130, 255), width=2)
            
            if item_img:
                # Resize and place item
                item_img_copy = item_img.copy()
                item_img_copy.thumbnail((slot_width - 15, slot_height - 35), Image.LANCZOS)
                item_x = x1 + (slot_width - item_img_copy.width) // 2
                item_y = y1 + 25 + (slot_height - 25 - item_img_copy.height) // 2
                canvas.paste(item_img_copy, (item_x, item_y), item_img_copy)
            else:
                # Draw placeholder text
                draw.text((x1 + slot_width//2, y1 + slot_height//2), "No Item", 
                         fill=(150, 150, 170, 255), anchor="mm")
    
    # 3. Player Info Section
    try:
        # Try to load a nice font
        font_paths = [
            "arial.ttf",
            "DejaVuSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/System/Library/Fonts/Helvetica.ttc"
        ]
        
        font_large = None
        font_medium = None
        
        for font_path in font_paths:
            try:
                font_large = ImageFont.truetype(font_path, 28)
                font_medium = ImageFont.truetype(font_path, 18)
                break
            except:
                continue
        
        if not font_large:
            font_large = ImageFont.load_default()
            font_medium = ImageFont.load_default()
    except:
        font_large = ImageFont.load_default()
        font_medium = ImageFont.load_default()
    
    # Info panel background
    draw.rectangle([0, 520, 800, 600], fill=(20, 20, 35, 255))
    draw.line([0, 520, 800, 520], fill=(100, 100, 150, 255), width=3)
    
    # Title
    title = "FREE FIRE OUTFIT SHOWCASE"
    bbox = draw.textbbox((0, 0), title, font=font_large)
    title_w = bbox[2] - bbox[0]
    draw.text((400 - title_w//2, 30), title, fill=(255, 200, 100, 255), font=font_large)
    
    # Player details
    y_start = 535
    line_height = 22
    
    # Line 1: Player Name
    name_text = f"Player: {player_name}"
    draw.text((50, y_start), name_text, fill=(255, 255, 255, 255), font=font_medium)
    
    # Line 2: UID and Level
    uid_level_text = f"UID: {uid} | Level: {player_level}"
    draw.text((50, y_start + line_height), uid_level_text, fill=(200, 200, 220, 255), font=font_medium)
    
    # Line 3: Region and Likes
    region_likes_text = f"Region: {player_region} | Likes: {player_likes}"
    draw.text((50, y_start + line_height * 2), region_likes_text, fill=(200, 200, 220, 255), font=font_medium)
    
    # Right side: Outfit count
    outfit_count = len([id for id in outfit_ids if id])
    count_text = f"Outfits: {outfit_count}/6"
    bbox = draw.textbbox((0, 0), count_text, font=font_medium)
    count_w = bbox[2] - bbox[0]
    draw.text((750 - count_w, y_start), count_text, fill=(150, 255, 150, 255), font=font_medium)
    
    # Timestamp
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    timestamp_text = f"Updated: {timestamp}"
    bbox = draw.textbbox((0, 0), timestamp_text, font=font_medium)
    timestamp_w = bbox[2] - bbox[0]
    draw.text((750 - timestamp_w, y_start + line_height * 2), timestamp_text, 
             fill=(150, 150, 180, 255), font=font_medium)
    
    return canvas

@app.route('/mafu-outfit-image', methods=['GET'])
def outfit_image():
    """Main endpoint for outfit image generation"""
    uid = request.args.get('uid')
    key = request.args.get('key')
    force_refresh = request.args.get('refresh', 'false').lower() == 'true'

    # Authentication
    if key != API_KEY:
        return jsonify({'error': 'Invalid or missing API key', 'status': 401}), 401
    
    if not uid:
        return jsonify({'error': 'Missing UID parameter', 'status': 400}), 400
    
    # Validate UID
    if not uid.isdigit() or len(uid) < 6:
        return jsonify({'error': 'Invalid UID format', 'status': 400}), 400
    
    # Clear cache if force refresh
    if force_refresh:
        if uid in player_cache:
            del player_cache[uid]
        image_cache.clear()

    # Fetch player data
    def fetch_func():
        return fetch_player_info(uid)
    
    player_data = get_cached_or_fetch(player_cache, uid, fetch_func)
    
    if not player_data:
        return jsonify({
            'error': 'Player Not Found',
            'message': 'Could not fetch player data',
            'uid': uid,
            'status': 404
        }), 404

    try:
        # Generate image
        result_image = create_outfit_image(player_data, uid)
        
        if not result_image:
            return jsonify({'error': 'Image generation failed', 'status': 500}), 500
        
        # Save to BytesIO
        img_io = BytesIO()
        result_image.save(img_io, 'PNG', optimize=True)
        img_io.seek(0)
        
        # Return image
        response = send_file(img_io, mimetype='image/png')
        response.headers['Cache-Control'] = f'public, max-age={CACHE_DURATION}'
        response.headers['X-UID'] = uid
        response.headers['X-Player'] = player_data.get('name', 'Unknown')
        response.headers['X-Generated'] = datetime.now().isoformat()
        
        return response
        
    except Exception as e:
        app.logger.error(f"Image generation error for UID {uid}: {str(e)}")
        return jsonify({
            'error': 'Image generation failed',
            'message': 'Internal server error',
            'status': 500
        }), 500

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'cache': {
            'players': len(player_cache),
            'images': len(image_cache)
        }
    })

@app.route('/player-info', methods=['GET'])
def get_player_info():
    """Get raw player info for debugging"""
    uid = request.args.get('uid')
    key = request.args.get('key')
    
    if key != API_KEY:
        return jsonify({'error': 'Invalid API key'}), 401
    
    if not uid:
        return jsonify({'error': 'Missing UID'}), 400
    
    player_data = fetch_player_info(uid)
    return jsonify(player_data)

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)