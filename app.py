from flask import Flask, request, jsonify, send_file
import requests
from PIL import Image
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import time
import hashlib
from functools import wraps
import logging

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
executor = ThreadPoolExecutor(max_workers=10)
session = requests.Session()

# --- Configuration ---
API_KEY = "MAFU"             # Expected API key
BACKGROUND_FILENAME = "outfit.png"  # local background image
IMAGE_TIMEOUT = 10                   # seconds for HTTP requests
CANVAS_SIZE = (800, 800)            # final image (width, height)
BACKGROUND_MODE = 'cover'           # 'cover' or 'contain'
CACHE_ENABLED = True                # Enable caching for better performance
CACHE_DURATION = 300                # Cache duration in seconds (5 minutes)

# Simple in-memory cache
image_cache = {}

def cache_response(ttl=CACHE_DURATION):
    """Cache decorator for API responses"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Create cache key from request
            cache_key = f"{func.__name__}:{request.args.get('uid')}"
            
            if CACHE_ENABLED and cache_key in image_cache:
                cached_data, timestamp = image_cache[cache_key]
                if time.time() - timestamp < ttl:
                    logger.info(f"Cache hit for UID: {request.args.get('uid')}")
                    return cached_data
            
            # Execute function
            result = func(*args, **kwargs)
            
            # Store in cache if successful
            if CACHE_ENABLED and result and isinstance(result, tuple) and result[1] == 200:
                image_cache[cache_key] = (result, time.time())
            
            return result
        return wrapper
    return decorator

def fetch_player_info(uid: str):
    """Fetch real-time player information from the API"""
    if not uid:
        return None
    
    # Primary API endpoint
    player_info_url = f"https://mafuuuu-info-api.vercel.app/mafu-info?uid={uid}"
    
    # Backup API endpoint (in case primary fails)
    backup_url = f"https://mafuuuu-info-api.vercel.app/api/player?uid={uid}"
    
    try:
        logger.info(f"Fetching player info for UID: {uid}")
        
        # Try primary API
        resp = session.get(player_info_url, timeout=IMAGE_TIMEOUT)
        
        if resp.status_code == 200:
            data = resp.json()
            logger.info(f"Successfully fetched data for UID: {uid}")
            return data
        else:
            logger.warning(f"Primary API returned {resp.status_code}, trying backup...")
            
            # Try backup API
            resp = session.get(backup_url, timeout=IMAGE_TIMEOUT)
            resp.raise_for_status()
            logger.info(f"Backup API successful for UID: {uid}")
            return resp.json()
            
    except requests.RequestException as e:
        logger.error(f"Error fetching player info for UID {uid}: {e}")
        return None

def fetch_and_process_image(image_url: str, size: tuple = None, retry_count=2):
    """Fetch image from URL with retry logic and optional resize"""
    for attempt in range(retry_count):
        try:
            resp = session.get(image_url, timeout=IMAGE_TIMEOUT)
            resp.raise_for_status()
            img = Image.open(BytesIO(resp.content)).convert("RGBA")
            if size:
                img = img.resize(size, Image.Resampling.LANCZOS)
            return img
        except Exception as e:
            logger.warning(f"Attempt {attempt + 1} failed for {image_url}: {e}")
            if attempt == retry_count - 1:
                return None
            time.sleep(0.5)  # Small delay before retry
    return None

def extract_outfit_ids(player_data: dict):
    """Extract outfit IDs from player data with multiple fallback methods"""
    outfit_ids = []
    
    # Method 1: Standard path
    if "AccountProfileInfo" in player_data:
        outfit_ids = player_data.get("AccountProfileInfo", {}).get("EquippedOutfit", [])
    
    # Method 2: Alternative path
    elif "profileInfo" in player_data:
        profile_info = player_data.get("profileInfo", {})
        outfit_ids = profile_info.get("EquippedOutfit", [])
        
        # Some APIs use different key names
        if not outfit_ids:
            outfit_ids = profile_info.get("outfits", [])
            if not outfit_ids:
                outfit_ids = profile_info.get("equippedItems", [])
    
    # Method 3: Basic info path
    elif "basicInfo" in player_data:
        basic_info = player_data.get("basicInfo", {})
        outfit_ids = basic_info.get("EquippedOutfit", [])
        if not outfit_ids:
            outfit_ids = basic_info.get("outfitIds", [])
    
    # Method 4: Direct array
    elif isinstance(player_data, list):
        outfit_ids = player_data
    
    # Method 5: Check for outfit data in any nested structure
    else:
        def find_outfit_ids(obj, depth=0):
            if depth > 3:  # Limit recursion depth
                return []
            if isinstance(obj, dict):
                for key, value in obj.items():
                    if key.lower() in ['equippedoutfit', 'outfits', 'outfitids', 'equippeditems']:
                        if isinstance(value, list):
                            return value
                    result = find_outfit_ids(value, depth + 1)
                    if result:
                        return result
            return []
        
        outfit_ids = find_outfit_ids(player_data)
    
    # Convert to strings and filter
    outfit_ids = [str(oid) for oid in outfit_ids if oid]
    
    logger.info(f"Extracted {len(outfit_ids)} outfit IDs: {outfit_ids[:5]}...")
    return outfit_ids

def get_player_name(player_data: dict):
    """Extract player name from various possible locations"""
    name_locations = [
        ("basicInfo", "nickname"),
        ("basicInfo", "name"),
        ("profileInfo", "nickname"),
        ("profileInfo", "playerName"),
        ("AccountBasicInfo", "nickname"),
        ("AccountBasicInfo", "name")
    ]
    
    for section, field in name_locations:
        if section in player_data:
            name = player_data.get(section, {}).get(field)
            if name:
                return name
    
    return "Unknown Player"

@app.route('/mafu-outfit-image', methods=['GET'])
@cache_response(ttl=CACHE_DURATION)
def outfit_image():
    """Generate real-time outfit combination image for a player"""
    start_time = time.time()
    
    uid = request.args.get('uid')
    key = request.args.get('key')
    refresh = request.args.get('refresh', 'false').lower() == 'true'  # Force refresh
    
    # Clear cache for this UID if refresh requested
    if refresh and uid:
        cache_key = f"outfit_image:{uid}"
        if cache_key in image_cache:
            del image_cache[cache_key]
            logger.info(f"Cache cleared for UID: {uid}")
    
    # API key validation
    if key != API_KEY:
        logger.warning(f"Invalid API key attempt from {request.remote_addr}")
        return jsonify({'error': 'Invalid or missing API key'}), 401
    
    if not uid:
        return jsonify({'error': 'Missing uid parameter'}), 400
    
    logger.info(f"Processing request for UID: {uid} (Refresh: {refresh})")
    
    # Fetch real-time player data
    player_data = fetch_player_info(uid)
    if player_data is None:
        logger.error(f"Failed to fetch player info for UID: {uid}")
        return jsonify({'error': 'Failed to fetch player info. Please check UID or try again.'}), 500
    
    # Extract outfit IDs from real-time data
    outfit_ids = extract_outfit_ids(player_data)
    
    if not outfit_ids:
        logger.warning(f"No outfit IDs found for UID: {uid}")
        # Don't return error, use fallbacks
    
    # Define required outfit codes and fallbacks (based on Free Fire outfit types)
    required_starts = ["211", "214", "208", "203", "204", "205", "212"]
    outfit_names = ["Head", "Face", "Mask", "Top", "Bottom", "Shoes", "Back"]
    fallback_ids = ["211000000", "214000000", "208000000", "203000000", 
                   "204000000", "205000000", "212000000"]
    
    used_ids = set()
    
    def fetch_outfit_image(idx, code):
        """Fetch outfit image for a specific code"""
        matched = None
        
        # Try to find matching outfit from player's real data
        for oid in outfit_ids:
            try:
                str_oid = str(oid)
                if str_oid.startswith(code) and str_oid not in used_ids:
                    matched = str_oid
                    used_ids.add(str_oid)
                    logger.info(f"Found real outfit for {outfit_names[idx]}: {matched}")
                    break
            except Exception:
                continue
        
        # Use fallback if no match found
        if matched is None:
            matched = fallback_ids[idx]
            logger.info(f"Using fallback outfit for {outfit_names[idx]}: {matched}")
        
        # Use multiple icon API endpoints for redundancy
        image_urls = [
            f'https://iconapi.wasmer.app/{matched}',
            f'https://ff.garena.com/api/icon/{matched}',
            f'https://api.duniagames.co.id/api/ff/icon/{matched}'
        ]
        
        # Try each URL until one works
        for url in image_urls:
            img = fetch_and_process_image(url, size=(150, 150))
            if img:
                return img
        
        return None
    
    # Submit all fetch tasks in parallel
    futures = []
    for idx, code in enumerate(required_starts):
        future = executor.submit(fetch_outfit_image, idx, code)
        futures.append(future)
    
    # Load local background image
    bg_path = os.path.join(os.path.dirname(__file__), BACKGROUND_FILENAME)
    try:
        background_image = Image.open(bg_path).convert("RGBA")
        logger.info(f"Background loaded: {background_image.size}")
    except FileNotFoundError:
        logger.error(f"Background image not found: {BACKGROUND_FILENAME}")
        return jsonify({'error': f'Background image not found: {BACKGROUND_FILENAME}'}), 500
    except Exception as e:
        logger.error(f"Failed to open background image: {e}")
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
        background_resized = background_image.resize((new_w, new_h), Image.Resampling.LANCZOS)
        
        offset_x = (canvas_w - new_w) // 2
        offset_y = (canvas_h - new_h) // 2
        scale_x = new_w / bg_w
        scale_y = new_h / bg_h
    
    # Create canvas and paste background
    canvas = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 255))
    canvas.paste(background_resized, (offset_x, offset_y), background_resized)
    
    # Add player name overlay
    try:
        from PIL import ImageDraw, ImageFont
        
        draw = ImageDraw.Draw(canvas)
        player_name = get_player_name(player_data)
        
        # Try to load font, fallback to default
        try:
            font = ImageFont.truetype("arial.ttf", 30)
        except:
            font = ImageFont.load_default()
        
        # Draw name background
        name_bbox = draw.textbbox((0, 0), player_name, font=font)
        name_width = name_bbox[2] - name_bbox[0]
        name_x = (canvas_w - name_width) // 2
        name_y = canvas_h - 50
        
        # Draw semi-transparent background
        draw.rectangle([name_x - 10, name_y - 5, name_x + name_width + 10, name_y + 35], 
                      fill=(0, 0, 0, 128))
        draw.text((name_x, name_y), player_name, fill=(255, 255, 255), font=font)
    except Exception as e:
        logger.warning(f"Could not add player name: {e}")
    
    # Positions for each outfit piece
    positions = [
        {'x': 350, 'y': 30, 'height': 150, 'width': 150},   # head
        {'x': 575, 'y': 130, 'height': 150, 'width': 150},  # face
        {'x': 665, 'y': 350, 'height': 150, 'width': 150},  # mask
        {'x': 575, 'y': 550, 'height': 150, 'width': 150},  # top
        {'x': 350, 'y': 654, 'height': 150, 'width': 150},  # bottom
        {'x': 135, 'y': 570, 'height': 150, 'width': 150},  # shoes
        {'x': 135, 'y': 130, 'height': 150, 'width': 150}   # back
    ]
    
    # Paste each fetched outfit image
    successful_pastes = 0
    for idx, future in enumerate(futures):
        try:
            outfit_img = future.result(timeout=IMAGE_TIMEOUT)
            if not outfit_img:
                logger.warning(f"Failed to fetch outfit {outfit_names[idx]}")
                continue
            
            pos = positions[idx]
            paste_x = offset_x + int(pos['x'] * scale_x)
            paste_y = offset_y + int(pos['y'] * scale_y)
            paste_w = max(1, int(pos['width'] * scale_x))
            paste_h = max(1, int(pos['height'] * scale_y))
            
            resized = outfit_img.resize((paste_w, paste_h), Image.Resampling.LANCZOS)
            canvas.paste(resized, (paste_x, paste_y), resized)
            successful_pastes += 1
            
        except Exception as e:
            logger.error(f"Error pasting outfit {outfit_names[idx]}: {e}")
            continue
    
    logger.info(f"Successfully pasted {successful_pastes}/7 outfit pieces for UID: {uid}")
    
    # Add watermark/timestamp
    try:
        draw = ImageDraw.Draw(canvas)
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        small_font = ImageFont.load_default()
        draw.text((10, canvas_h - 20), f"Generated: {timestamp}", fill=(255, 255, 255, 128), font=small_font)
    except:
        pass
    
    # Output PNG
    output = BytesIO()
    canvas.save(output, format='PNG', optimize=True)
    output.seek(0)
    
    processing_time = time.time() - start_time
    logger.info(f"Request completed in {processing_time:.2f} seconds for UID: {uid}")
    
    return send_file(
        output, 
        mimetype='image/png',
        headers={
            'X-Processing-Time': str(processing_time),
            'X-Outfits-Found': str(len(outfit_ids)),
            'X-Outfits-Pasted': str(successful_pastes),
            'Cache-Control': 'public, max-age=300'
        }
    )

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint with real-time status"""
    return jsonify({
        'status': 'healthy',
        'message': 'Outfit API is running with real-time data',
        'cache_enabled': CACHE_ENABLED,
        'cache_size': len(image_cache),
        'timestamp': time.time()
    }), 200

@app.route('/clear-cache', methods=['POST'])
def clear_cache():
    """Clear the image cache"""
    key = request.args.get('key')
    
    if key != API_KEY:
        return jsonify({'error': 'Invalid API key'}), 401
    
    cache_size = len(image_cache)
    image_cache.clear()
    
    return jsonify({
        'message': f'Cache cleared successfully',
        'cleared_items': cache_size
    }), 200

@app.route('/', methods=['GET'])
def home():
    """Home endpoint with API documentation"""
    return jsonify({
        'name': 'MAFU Outfit Image Generator API',
        'version': '2.0.0',
        'description': 'Real-time Free Fire outfit combination image generator',
        'endpoints': {
            '/mafu-outfit-image': {
                'method': 'GET',
                'description': 'Generate outfit image from real-time player data',
                'parameters': {
                    'uid': 'Player UID (required)',
                    'key': 'API key: MAFU (required)',
                    'refresh': 'Set "true" to bypass cache (optional)'
                },
                'example': '/mafu-outfit-image?uid=123456789&key=MAFU'
            },
            '/health': {
                'method': 'GET',
                'description': 'Health check endpoint'
            },
            '/clear-cache': {
                'method': 'POST',
                'description': 'Clear image cache (requires API key)',
                'parameters': {
                    'key': 'API key: MAFU (required)'
                }
            }
        },
        'features': [
            'Real-time data fetching',
            'Intelligent caching for performance',
            'Multiple API fallbacks',
            'Automatic outfit detection',
            'Player name overlay',
            'Processing time tracking'
        ],
        'made_by': 'MAFU',
        'telegram': '@mahfuj_offcial_143'
    })

if __name__ == '__main__':
    print("""
    ╔══════════════════════════════════════════════════════════════╗
    ║                                                              ║
    ║     🎨 MAFU OUTFIT IMAGE GENERATOR API v2.0 🎨              ║
    ║                                                              ║
    ║     ✓ Real-time data fetching                               ║
    ║     ✓ Intelligent caching                                   ║
    ║     ✓ Multiple API fallbacks                                ║
    ║     ✓ Automatic outfit detection                            ║
    ║                                                              ║
    ║     Running on: http://0.0.0.0:5000                        ║
    ║     API Key: MAFU                                           ║
    ║                                                              ║
    ║     Made with ❤️ by MAFU                                    ║
    ║     Telegram: @mahfuj_offcial_143                          ║
    ║                                                              ║
    ╚══════════════════════════════════════════════════════════════╝
    """)
    
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
