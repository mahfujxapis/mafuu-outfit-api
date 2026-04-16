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

# Cache configuration - SHORTER for real-time data
CACHE_DURATION = 60  # 1 minute for real-time feel
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

def get_cached_or_fetch(cache_dict, key, fetch_func, *args, **kwargs):
    """Generic cache handler with real-time priority"""
    if key in cache_dict:
        cached_data, timestamp = cache_dict[key]
        if datetime.now() - timestamp < timedelta(seconds=CACHE_DURATION):
            return cached_data
    
    data = fetch_func(*args, **kwargs)
    if data:
        cache_dict[key] = (data, datetime.now())
    return data

def extract_player_data(raw_data):
    """Extract player data from API response - FIXED for real API structure"""
    player_info = {
        'name': 'Unknown Player',
        'level': '?',
        'uid': '',
        'region': 'Unknown',
        'likes': '0',
        'avatar_url': None,
        'outfit_ids': [],
        'banner_url': None,
        'rank': None,
        'cs_rank': None
    }
    
    if not raw_data:
        return player_info
    
    # Debug: Print raw data structure
    print(f"[DEBUG] Raw data type: {type(raw_data)}")
    if isinstance(raw_data, dict):
        print(f"[DEBUG] Raw data keys: {list(raw_data.keys())}")
    
    # Structure 1: mafu-info API format (result wrapper)
    if isinstance(raw_data, dict):
        # Check for nested result structure
        data = raw_data.get('result', raw_data)
        
        # Account Info section
        if 'AccountInfo' in data:
            account = data['AccountInfo']
            player_info['name'] = account.get('AccountName', 'Unknown Player')
            player_info['level'] = str(account.get('AccountLevel', '?'))
            player_info['uid'] = str(account.get('AccountId', ''))
            player_info['region'] = account.get('AccountRegion', 'Unknown')
            player_info['likes'] = str(account.get('AccountLikes', '0'))
            player_info['avatar_url'] = f"https://freefiremobile-a.akamaihd.net/common/avatar/{account.get('AccountAvatarId', '')}.png" if account.get('AccountAvatarId') else None
            player_info['banner_url'] = f"https://freefiremobile-a.akamaihd.net/common/banner/{account.get('AccountBannerId', '')}.png" if account.get('AccountBannerId') else None
            player_info['rank'] = account.get('BrMaxRank')
            player_info['cs_rank'] = account.get('CsMaxRank')
        
        # Account Profile Info section - OUTFITS
        if 'AccountProfileInfo' in data:
            profile = data['AccountProfileInfo']
            player_info['outfit_ids'] = profile.get('EquippedOutfit', [])
        
        # Alternative: direct clothes field
        if 'clothes' in data:
            player_info['outfit_ids'] = data.get('clothes', [])
        
        # Guild Info
        if 'GuildInfo' in data:
            guild = data['GuildInfo']
            player_info['guild_name'] = guild.get('GuildName', '')
            player_info['guild_level'] = guild.get('GuildLevel', 0)
        
        # Pet Info
        if 'petInfo' in data:
            pet = data['petInfo']
            player_info['pet_id'] = pet.get('id')
            player_info['pet_level'] = pet.get('level')
    
    return player_info

def fetch_player_info(uid: str, region: str = None):
    """Fetch player info with retry and better error handling - REAL TIME"""
    if not uid:
        return None
    
    # Build URL with optional region
    player_info_url = f"{PLAYER_INFO_API}?uid={uid}"
    if region:
        player_info_url += f"&region={region}"
    
    print(f"[DEBUG] Fetching: {player_info_url}")
    
    for attempt in range(3):
        try:
            resp = session.get(
                player_info_url, 
                timeout=IMAGE_TIMEOUT, 
                headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    'Accept': 'application/json',
                    'Cache-Control': 'no-cache'  # Force fresh data
                }
            )
            resp.raise_for_status()
            
            # Try to parse JSON
            try:
                data = resp.json()
            except json.JSONDecodeError as e:
                print(f"[ERROR] Attempt {attempt + 1}: Invalid JSON - {e}")
                print(f"[ERROR] Response text: {resp.text[:200]}")
                continue
            
            # Extract and validate data
            player_data = extract_player_data(data)
            player_data['fetch_time'] = datetime.now().isoformat()
            
            # Check if we got meaningful data
            if player_data['name'] != 'Unknown Player':
                print(f"[DEBUG] Successfully fetched player: {player_data['name']}")
                return player_data
            
            print(f"[WARN] Attempt {attempt + 1}: Incomplete data received")
            
        except requests.RequestException as e:
            print(f"[ERROR] Attempt {attempt + 1}: Request failed - {e}")
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
        'banner_url': None,
        'fetch_time': datetime.now().isoformat(),
        'error': 'API fetch failed'
    }

def fetch_and_process_image(image_url: str, size: tuple = None):
    """Fetch and process image with caching"""
    if not image_url:
        return None
    
    cache_key = f"{image_url}_{size}"
    
    def fetch_image():
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'image/webp,image/apng,image/*,*/*;q=0.8',
                'Cache-Control': 'no-cache'
            }
            resp = session.get(image_url, timeout=IMAGE_TIMEOUT, headers=headers)
            resp.raise_for_status()
            img = Image.open(BytesIO(resp.content)).convert("RGBA")
            if size:
                img.thumbnail(size, Image.LANCZOS)
            return img
        except Exception as e:
            print(f"[ERROR] Image fetch error for {image_url}: {e}")
            return None
    
    return get_cached_or_fetch(image_cache, cache_key, fetch_image)

def create_character_placeholder():
    """Create a stylish placeholder character"""
    img = Image.new('RGBA', (280, 420), (45, 45, 65, 255))
    draw = ImageDraw.Draw(img)
    
    # Character silhouette with gradient effect
    draw.ellipse([90, 50, 190, 150], fill=(120, 120, 150, 255), outline=(180, 180, 200, 255), width=2)
    draw.rectangle([100, 150, 180, 300], fill=(100, 100, 130, 255), outline=(160, 160, 180, 255), width=2)
    draw.rectangle([60, 160, 100, 250], fill=(100, 100, 130, 255), outline=(160, 160, 180, 255), width=2)
    draw.rectangle([180, 160, 220, 250], fill=(100, 100, 130, 255), outline=(160, 160, 180, 255), width=2)
    draw.rectangle([110, 300, 145, 400], fill=(90, 90, 120, 255), outline=(150, 150, 170, 255), width=2)
    draw.rectangle([155, 300, 190, 400], fill=(90, 90, 120, 255), outline=(150, 150, 170, 255), width=2)
    draw.text((140, 210), "?", fill=(255, 255, 255, 255), anchor="mm")
    
    return img

def get_character_image(uid: str, avatar_url: str = None):
    """Fetch character image with multiple sources"""
    # Try avatar URL first
    if avatar_url:
        img = fetch_and_process_image(avatar_url, (280, 420))
        if img:
            return img
    
    # Try character API
    char_url = f"{CHARACTER_API}?uid={uid}"
    img = fetch_and_process_image(char_url, (280, 420))
    if img:
        return img
    
    # Return stylish placeholder
    return create_character_placeholder()

def fetch_outfit_items_parallel(outfit_ids):
    """Fetch multiple outfit items in parallel - REAL TIME"""
    if not outfit_ids:
        return []
    
    futures = []
    valid_outfits = [oid for oid in outfit_ids[:6] if oid]
    
    for item_id in valid_outfits:
        item_url = f"{ASSET_BASE_URL}{item_id}"
        future = executor.submit(fetch_and_process_image, item_url, (100, 100))
        futures.append((item_id, future))
    
    results = []
    for item_id, future in futures:
        try:
            img = future.result(timeout=IMAGE_TIMEOUT)
            results.append(img)
        except Exception as e:
            print(f"[ERROR] Failed to fetch outfit item {item_id}: {e}")
            results.append(None)
    
    # Pad results to match slot positions
    while len(results) < 6:
        results.append(None)
    
    return results[:6]

def create_outfit_image(player_data: dict, uid: str):
    """Create outfit showcase image with real-time data"""
    
    # Load or create background
    if os.path.exists(BACKGROUND_FILENAME):
        canvas = Image.open(BACKGROUND_FILENAME).convert("RGBA")
        if canvas.size != CANVAS_SIZE:
            canvas = canvas.resize(CANVAS_SIZE, Image.LANCZOS)
    else:
        canvas = Image.new('RGBA', CANVAS_SIZE, (25, 25, 40, 255))
        draw = ImageDraw.Draw(canvas)
        draw.rectangle([5, 5, 795, 595], outline=(120, 120, 160, 255), width=3)
        draw.rectangle([10, 10, 790, 590], outline=(80, 80, 120, 255), width=1)
    
    draw = ImageDraw.Draw(canvas)
    
    # Extract player info
    player_name = player_data.get('name', 'Unknown Player')
    player_level = player_data.get('level', '?')
    player_region = player_data.get('region', 'Unknown')
    player_likes = player_data.get('likes', '0')
    avatar_url = player_data.get('avatar_url')
    outfit_ids = player_data.get('outfit_ids', [])
    rank = player_data.get('rank', '')
    guild_name = player_data.get('guild_name', '')
    
    # 1. Place Character
    char_img = get_character_image(uid, avatar_url)
    if char_img:
        char_x = (CHARACTER_BOX[0] + CHARACTER_BOX[2]) // 2 - char_img.width // 2
        char_y = (CHARACTER_BOX[1] + CHARACTER_BOX[3]) // 2 - char_img.height // 2
        canvas.paste(char_img, (char_x, char_y), char_img)
    
    # 2. Place Outfit Items - REAL TIME FETCH
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
            item_x = x1 + (slot_width - item_img.width) // 2
            item_y = y1 + (slot_height - item_img.height) // 2
            canvas.paste(item_img, (item_x, item_y), item_img)
        else:
            draw.text((x1 + slot_width//2, y1 + slot_height//2), "Empty", 
                     fill=(150, 150, 170, 255), anchor="mm")
    
    # 3. Player Info Section
    try:
        font_paths = [
            "arial.ttf",
            "DejaVuSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/System/Library/Fonts/Helvetica.ttc"
        ]
        
        font_large = None
        font_medium = None
        font_small = None
        
        for font_path in font_paths:
            try:
                font_large = ImageFont.truetype(font_path, 28)
                font_medium = ImageFont.truetype(font_path, 18)
                font_small = ImageFont.truetype(font_path, 14)
                break
            except:
                continue
        
        if not font_large:
            font_large = ImageFont.load_default()
            font_medium = ImageFont.load_default()
            font_small = font_medium
    except:
        font_large = ImageFont.load_default()
        font_medium = ImageFont.load_default()
        font_small = font_medium
    
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
    if rank:
        uid_level_text += f" | Rank: {rank}"
    draw.text((50, y_start + line_height), uid_level_text, fill=(200, 200, 220, 255), font=font_medium)
    
    # Line 3: Region, Likes, Guild
    region_text = f"Region: {player_region} | Likes: {player_likes}"
    if guild_name:
        region_text += f" | Guild: {guild_name}"
    draw.text((50, y_start + line_height * 2), region_text, fill=(200, 200, 220, 255), font=font_medium)
    
    # Right side: Outfit count
    outfit_count = len([oid for oid in outfit_ids if oid])
    count_text = f"Outfits: {outfit_count}/6"
    bbox = draw.textbbox((0, 0), count_text, font=font_medium)
    count_w = bbox[2] - bbox[0]
    draw.text((750 - count_w, y_start), count_text, fill=(150, 255, 150, 255), font=font_medium)
    
    # Real-time timestamp
    fetch_time = player_data.get('fetch_time', datetime.now().isoformat())
    timestamp_text = f"Live: {fetch_time[:19]}"
    bbox = draw.textbbox((0, 0), timestamp_text, font=font_small)
    timestamp_w = bbox[2] - bbox[0]
    draw.text((750 - timestamp_w, y_start + line_height * 2), timestamp_text, 
             fill=(100, 255, 100, 255), font=font_small)
    
    return canvas

@app.route('/mafu-outfit-image', methods=['GET'])
def outfit_image():
    """Main endpoint for outfit image generation - REAL TIME"""
    uid = request.args.get('uid')
    key = request.args.get('key')
    region = request.args.get('region')  # Optional region parameter
    force_refresh = request.args.get('refresh', 'false').lower() == 'true'
    no_cache = request.args.get('nocache', 'false').lower() == 'true'

    # Authentication
    if key != API_KEY:
        return jsonify({'error': 'Invalid or missing API key', 'status': 401}), 401
    
    if not uid:
        return jsonify({'error': 'Missing UID parameter', 'status': 400}), 400
    
    # Validate UID
    if not uid.isdigit() or len(uid) < 6:
        return jsonify({'error': 'Invalid UID format', 'status': 400}), 400
    
    # Clear cache if force refresh
    if force_refresh or no_cache:
        if uid in player_cache:
            del player_cache[uid]
        if no_cache:
            image_cache.clear()

    # Fetch player data - REAL TIME
    def fetch_func():
        return fetch_player_info(uid, region)
    
    # Skip cache for real-time requests
    if no_cache:
        player_data = fetch_func()
    else:
        player_data = get_cached_or_fetch(player_cache, uid, fetch_func)
    
    if not player_data:
        return jsonify({
            'error': 'Player Not Found',
            'message': 'Could not fetch player data from API',
            'uid': uid,
            'status': 404
        }), 404

    # Check if API returned an error
    if player_data.get('error') and player_data.get('name', '').startswith('Player_'):
        return jsonify({
            'error': 'Player Not Found',
            'message': f"API Error: {player_data.get('error')}",
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
        
        # Return image with no-cache headers for real-time
        response = send_file(img_io, mimetype='image/png')
        if no_cache:
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        else:
            response.headers['Cache-Control'] = f'public, max-age={CACHE_DURATION}'
        response.headers['X-UID'] = uid
        response.headers['X-Player'] = player_data.get('name', 'Unknown')
        response.headers['X-Generated'] = datetime.now().isoformat()
        response.headers['X-Data-Time'] = player_data.get('fetch_time', '')
        
        return response
        
    except Exception as e:
        app.logger.error(f"Image generation error for UID {uid}: {str(e)}")
        return jsonify({
            'error': 'Image generation failed',
            'message': str(e),
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
        },
        'real_time_mode': True
    })

@app.route('/player-info', methods=['GET'])
def get_player_info():
    """Get raw player info for debugging - REAL TIME"""
    uid = request.args.get('uid')
    key = request.args.get('key')
    region = request.args.get('region')
    no_cache = request.args.get('nocache', 'false').lower() == 'true'
    
    if key != API_KEY:
        return jsonify({'error': 'Invalid API key'}), 401
    
    if not uid:
        return jsonify({'error': 'Missing UID'}), 400
    
    if no_cache:
        player_data = fetch_player_info(uid, region)
    else:
        def fetch_func():
            return fetch_player_info(uid, region)
        player_data = get_cached_or_fetch(player_cache, uid, fetch_func)
    
    return jsonify({
        'data': player_data,
        'cached': uid in player_cache and not no_cache,
        'timestamp': datetime.now().isoformat()
    })

@app.route('/clear-cache', methods=['POST'])
def clear_cache():
    """Clear all caches for fresh data"""
    key = request.args.get('key')
    
    if key != API_KEY:
        return jsonify({'error': 'Invalid API key'}), 401
    
    player_cache.clear()
    image_cache.clear()
    
    return jsonify({
        'message': 'All caches cleared',
        'timestamp': datetime.now().isoformat()
    })

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)