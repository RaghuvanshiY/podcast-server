"""Main Flask web server for podcast ad removal."""
import logging
import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from flask import Flask, Response, send_file, abort
from slugify import slugify
import shutil

from storage import Storage
from rss_parser import RSSParser
from transcriber import Transcriber
from ad_detector import AdDetector
from audio_processor import AudioProcessor

# Configure logging to both file and console
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler('/app/data/server.log'),
        logging.StreamHandler()  # Keep console output for Docker logs
    ]
)
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)

# Initialize components
storage = Storage()
rss_parser = RSSParser()
transcriber = Transcriber()
ad_detector = AdDetector()
audio_processor = AudioProcessor()

# Load feed configuration
def load_feeds():
    """Load feed configuration from JSON."""
    config_path = Path("./config/feeds.json")
    if not config_path.exists():
        logger.error("feeds.json not found")
        return []

    try:
        with open(config_path, 'r') as f:
            feeds = json.load(f)
            logger.info(f"Loaded {len(feeds)} feed configurations")
            return feeds
    except Exception as e:
        logger.error(f"Failed to load feeds.json: {e}")
        return []

def reload_feeds():
    """Reload feed configuration and update global FEED_MAP."""
    global FEEDS, FEED_MAP
    FEEDS = load_feeds()
    FEED_MAP = {slugify(feed['out'].strip('/')): feed for feed in FEEDS}
    logger.info(f"Reloaded feeds: {list(FEED_MAP.keys())}")
    return FEED_MAP

# Initial load of feed configuration
FEEDS = load_feeds()
FEED_MAP = {slugify(feed['out'].strip('/')): feed for feed in FEEDS}

def refresh_rss_feed(slug: str, feed_url: str):
    """Refresh RSS feed for a podcast."""
    try:
        logger.info(f"[{slug}] Starting RSS refresh from: {feed_url}")

        # Fetch original RSS
        feed_content = rss_parser.fetch_feed(feed_url)
        if not feed_content:
            logger.error(f"[{slug}] Failed to fetch RSS feed")
            return False

        # Modify feed URLs
        modified_rss = rss_parser.modify_feed(feed_content, slug)

        # Save modified RSS
        storage.save_rss(slug, modified_rss)

        # Update last_checked timestamp
        data = storage.load_data_json(slug)
        data['last_checked'] = datetime.utcnow().isoformat() + 'Z'
        storage.save_data_json(slug, data)

        logger.info(f"[{slug}] RSS refresh complete")
        return True
    except Exception as e:
        logger.error(f"[{slug}] RSS refresh failed: {e}")
        return False

def refresh_all_feeds():
    """Refresh all RSS feeds once (no loop)."""
    try:
        logger.info("Refreshing all RSS feeds")
        # Reload feeds.json to pick up any changes
        reload_feeds()

        for slug, feed_info in FEED_MAP.items():
            refresh_rss_feed(slug, feed_info['in'])
        logger.info("RSS refresh complete")
        return True
    except Exception as e:
        logger.error(f"RSS refresh failed: {e}")
        return False

def background_rss_refresh():
    """Background task to refresh RSS feeds every 15 minutes."""
    while True:
        refresh_all_feeds()
        # Wait 15 minutes
        time.sleep(900)

def process_episode(slug: str, episode_id: str, episode_url: str, episode_title: str = "Unknown"):
    """Process a single episode (transcribe, detect ads, remove ads)."""
    start_time = time.time()

    try:
        # Log start with title
        logger.info(f"[{slug}:{episode_id}] Starting: \"{episode_title}\"")

        # Update status to processing
        data = storage.load_data_json(slug)
        data['episodes'][episode_id] = {
            'status': 'processing',
            'original_url': episode_url,
            'title': episode_title,
            'processed_at': datetime.utcnow().isoformat() + 'Z'
        }
        storage.save_data_json(slug, data)

        # Step 1: Check if transcript exists
        transcript_path = storage.get_episode_path(slug, episode_id, "-transcript.txt")
        segments = None
        transcript_text = None

        if transcript_path.exists():
            logger.info(f"[{slug}:{episode_id}] Found existing transcript, skipping transcription")
            # Load existing transcript
            with open(transcript_path, 'r') as f:
                transcript_text = f.read()
            # Parse segments from transcript
            segments = []
            for line in transcript_text.split('\n'):
                if line.strip() and line.startswith('['):
                    # Parse format: [00:00:00.000 --> 00:00:05.200] text
                    try:
                        time_part, text_part = line.split('] ', 1)
                        time_range = time_part.strip('[')
                        start_str, end_str = time_range.split(' --> ')
                        # Convert timestamp to seconds
                        def parse_timestamp(ts):
                            parts = ts.split(':')
                            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
                        segments.append({
                            'start': parse_timestamp(start_str),
                            'end': parse_timestamp(end_str),
                            'text': text_part
                        })
                    except:
                        continue

            if segments:
                segment_count = len(segments)
                duration_min = segments[-1]['end'] / 60 if segments else 0
                logger.info(f"[{slug}:{episode_id}] Loaded transcript: {segment_count} segments, {duration_min:.1f} minutes")

            # Still need to download audio for processing
            audio_path = transcriber.download_audio(episode_url)
            if not audio_path:
                raise Exception("Failed to download audio")
        else:
            # Download and transcribe
            logger.info(f"[{slug}:{episode_id}] Downloading audio")
            audio_path = transcriber.download_audio(episode_url)
            if not audio_path:
                raise Exception("Failed to download audio")

            logger.info(f"[{slug}:{episode_id}] Starting transcription")
            segments = transcriber.transcribe(audio_path)
            if not segments:
                raise Exception("Failed to transcribe audio")

            segment_count = len(segments)
            duration_min = segments[-1]['end'] / 60 if segments else 0
            logger.info(f"[{slug}:{episode_id}] Transcription completed: {segment_count} segments, {duration_min:.1f} minutes")

            # Save transcript
            transcript_text = transcriber.segments_to_text(segments)
            storage.save_transcript(slug, episode_id, transcript_text)

        try:

            # Step 2: Detect ads
            logger.info(f"[{slug}:{episode_id}] Sending to Claude API")
            ad_result = ad_detector.process_transcript(segments)
            storage.save_ads_json(slug, episode_id, ad_result)

            ads = ad_result.get('ads', [])
            if ads:
                total_ad_time = sum(ad['end'] - ad['start'] for ad in ads)
                logger.info(f"[{slug}:{episode_id}] Claude detected {len(ads)} ad segments (total {total_ad_time/60:.1f} minutes)")
            else:
                logger.info(f"[{slug}:{episode_id}] No ads detected")

            # Step 3: Process audio to remove ads
            logger.info(f"[{slug}:{episode_id}] Starting FFMPEG")
            processed_path = audio_processor.process_episode(audio_path, ads)
            if not processed_path:
                raise Exception("Failed to process audio with FFMPEG")

            # Get durations for logging
            original_duration = audio_processor.get_audio_duration(audio_path)
            new_duration = audio_processor.get_audio_duration(processed_path)

            # Move processed file to final location
            final_path = storage.get_episode_path(slug, episode_id)
            shutil.move(processed_path, final_path)

            # Update status to processed
            data = storage.load_data_json(slug)
            data['episodes'][episode_id] = {
                'status': 'processed',
                'original_url': episode_url,
                'title': episode_title,
                'processed_file': f"episodes/{episode_id}.mp3",
                'processed_at': datetime.utcnow().isoformat() + 'Z',
                'original_duration': original_duration,
                'new_duration': new_duration,
                'ads_removed': len(ads)
            }
            storage.save_data_json(slug, data)

            # Calculate processing time
            processing_time = time.time() - start_time

            # Final summary log
            if original_duration and new_duration:
                time_saved = original_duration - new_duration
                logger.info(f"[{slug}:{episode_id}] Complete: \"{episode_title}\" | {original_duration/60:.1f}→{new_duration/60:.1f}min | {len(ads)} ads removed | {processing_time:.1f}s")
            else:
                logger.info(f"[{slug}:{episode_id}] Complete: \"{episode_title}\" | {len(ads)} ads removed | {processing_time:.1f}s")

            return True

        finally:
            # Clean up temp audio file
            if os.path.exists(audio_path):
                os.unlink(audio_path)

    except Exception as e:
        processing_time = time.time() - start_time
        logger.error(f"[{slug}:{episode_id}] Failed: \"{episode_title}\" | Error: {e} | {processing_time:.1f}s")

        # Update status to failed
        data = storage.load_data_json(slug)
        data['episodes'][episode_id] = {
            'status': 'failed',
            'original_url': episode_url,
            'title': episode_title,
            'error': str(e),
            'failed_at': datetime.utcnow().isoformat() + 'Z'
        }
        storage.save_data_json(slug, data)
        return False

@app.route('/<slug>')
def serve_rss(slug):
    """Serve modified RSS feed."""
    if slug not in FEED_MAP:
        # Refresh all feeds to pick up any new ones
        logger.info(f"[{slug}] Not found in feeds, refreshing all")
        refresh_all_feeds()

        # Check again after refresh
        if slug not in FEED_MAP:
            logger.warning(f"[{slug}] Still not found after refresh")
            abort(404)

    # Check if RSS cache exists or is stale
    cached_rss = storage.get_rss(slug)
    data = storage.load_data_json(slug)
    last_checked = data.get('last_checked')

    # If no cache or stale (>15 min), refresh immediately
    should_refresh = False
    if not cached_rss:
        should_refresh = True
        logger.info(f"[{slug}] No RSS cache, fetching immediately")
    elif last_checked:
        try:
            last_time = datetime.fromisoformat(last_checked.replace('Z', '+00:00'))
            age_minutes = (datetime.utcnow() - last_time.replace(tzinfo=None)).total_seconds() / 60
            if age_minutes > 15:
                should_refresh = True
                logger.info(f"[{slug}] RSS cache stale ({age_minutes:.1f} minutes old), refreshing")
        except:
            should_refresh = True

    if should_refresh:
        refresh_rss_feed(slug, FEED_MAP[slug]['in'])
        cached_rss = storage.get_rss(slug)

    if cached_rss:
        logger.info(f"[{slug}] Serving RSS feed")
        return Response(cached_rss, mimetype='application/rss+xml')
    else:
        logger.error(f"[{slug}] RSS feed not available")
        abort(503)

@app.route('/episodes/<slug>/<episode_id>.mp3')
def serve_episode(slug, episode_id):
    """Serve processed episode audio (JIT processing)."""
    if slug not in FEED_MAP:
        # Refresh all feeds to pick up any new ones
        logger.info(f"[{slug}] Not found in feeds for episode {episode_id}, refreshing all")
        refresh_all_feeds()

        # Check again after refresh
        if slug not in FEED_MAP:
            logger.warning(f"[{slug}] Still not found after refresh for episode {episode_id}")
            abort(404)

    # Validate episode ID (alphanumeric + dash/underscore)
    if not all(c.isalnum() or c in '-_' for c in episode_id):
        logger.warning(f"[{slug}] Invalid episode ID: {episode_id}")
        abort(400)

    # Check episode status
    data = storage.load_data_json(slug)
    episode_info = data['episodes'].get(episode_id, {})
    status = episode_info.get('status')

    if status == 'processed':
        # Serve cached processed file
        file_path = storage.get_episode_path(slug, episode_id)
        if file_path.exists():
            logger.info(f"[{slug}:{episode_id}] Cache hit, serving processed file")
            return send_file(file_path, mimetype='audio/mpeg')
        else:
            logger.error(f"[{slug}:{episode_id}] Processed file missing")
            status = None  # Reprocess

    elif status == 'failed':
        # Serve original file (no retry)
        original_url = episode_info.get('original_url')
        if original_url:
            logger.info(f"[{slug}:{episode_id}] Serving original fallback (previous failure)")
            # Redirect to original URL
            return Response(status=302, headers={'Location': original_url})
        else:
            abort(404)

    elif status == 'processing':
        # Already processing, return temporary unavailable
        logger.info(f"[{slug}:{episode_id}] Episode currently processing")
        abort(503)

    # Status is None or unknown - need to process
    # First, we need to find the original URL from the RSS feed
    cached_rss = storage.get_rss(slug)
    if not cached_rss:
        logger.error(f"[{slug}:{episode_id}] No RSS feed available")
        abort(404)

    # Parse RSS to find original URL
    original_feed = rss_parser.fetch_feed(FEED_MAP[slug]['in'])
    if not original_feed:
        logger.error(f"[{slug}:{episode_id}] Could not fetch original RSS")
        abort(503)

    episodes = rss_parser.extract_episodes(original_feed)
    original_url = None
    episode_title = "Unknown"
    for ep in episodes:
        if ep['id'] == episode_id:
            original_url = ep['url']
            episode_title = ep.get('title', 'Unknown')
            break

    if not original_url:
        logger.error(f"[{slug}:{episode_id}] Episode not found in RSS feed")
        abort(404)

    logger.info(f"[{slug}:{episode_id}] Starting new processing")

    # Process episode (blocking)
    if process_episode(slug, episode_id, original_url, episode_title):
        # Serve the newly processed file
        file_path = storage.get_episode_path(slug, episode_id)
        if file_path.exists():
            return send_file(file_path, mimetype='audio/mpeg')

    # Processing failed, serve original
    logger.info(f"[{slug}:{episode_id}] Processing failed, serving original")
    return Response(status=302, headers={'Location': original_url})

@app.route('/health')
def health_check():
    """Health check endpoint."""
    return {'status': 'ok', 'feeds': len(FEEDS)}

if __name__ == '__main__':
    # Log BASE_URL configuration
    base_url = os.getenv('BASE_URL', 'http://localhost:8000')
    logger.info(f"BASE_URL configured as: {base_url}")

    # Start background RSS refresh thread
    refresh_thread = threading.Thread(target=background_rss_refresh, daemon=True)
    refresh_thread.start()
    logger.info("Started background RSS refresh thread")

    # Do initial RSS refresh for all feeds
    logger.info("Performing initial RSS refresh for all feeds")
    for slug, feed_info in FEED_MAP.items():
        refresh_rss_feed(slug, feed_info['in'])
        logger.info(f"Feed available at: {base_url}/{slug}")

    # Start Flask server
    logger.info("Starting Flask server on port 8000")
    app.run(host='0.0.0.0', port=8000, debug=False)