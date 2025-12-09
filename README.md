# Podcast Ad Removal Server

Removes ads from podcasts using Whisper transcription. Serves modified RSS feeds that work with any podcast app.

> **Disclaimer:** This tool is for personal use only. Only use it with podcasts you have permission to modify or where such modification is permitted under applicable laws. Respect content creators and their terms of service.

## How It Works

1. **Transcription** - Whisper converts audio to text with timestamps
2. **Ad Detection** - Claude API analyzes transcript to identify ad segments
3. **Audio Processing** - FFmpeg removes detected ads and inserts short audio markers
4. **Serving** - Flask serves modified RSS feeds and processed audio files

Processing happens on-demand when you play an episode. First play takes a few minutes, subsequent plays are instant (cached).

## Requirements

- Docker with NVIDIA GPU support (for Whisper)
- Anthropic API key

### CPU-Only Mode

A [`cpu` branch](https://github.com/hemant6488/podcast-server/tree/cpu) is available that runs Whisper without an NVIDIA GPU.

**Important:** CPU transcription is significantly slower—processing can take longer than the episode duration. Since episodes are processed on-demand when you play them, your podcast app will likely timeout waiting for the first request. To work around this:

1. Tap download/play on an episode to trigger processing
2. The request will timeout, but processing continues in the background
3. Wait a few minutes (check `docker logs` for progress) for the file to get processed
4. Try playing again, this time the processed file will be served from cache

## Setup

```bash
# 1. Create environment file
echo "ANTHROPIC_API_KEY=your-key-here" > .env

# 2. Configure feeds
cp config/feeds-example.json config/feeds.json
# Edit config/feeds.json with your podcast RSS URLs

# 3. Run
docker-compose up --build
```

## Configuration

Edit `config/feeds.json`:
```json
[
  {
    "in": "https://example.com/podcast/feed.rss",
    "out": "/mypodcast"
  }
]
```

- `in` - Original podcast RSS feed URL
- `out` - URL path for your modified feed (e.g., `/mypodcast` → `http://localhost:8000/mypodcast`)

## Finding Podcast RSS Feeds

Most podcasts publish RSS feeds. Common ways to find them:

1. **Podcast website** - Look for "RSS" link in footer or subscription options
2. **Apple Podcasts** - Search on [podcastindex.org](https://podcastindex.org) using the Apple Podcasts URL
3. **Spotify-exclusive** - Not available (Spotify doesn't expose RSS feeds)
4. **Hosting platforms** - Common patterns:
   - Libsyn: `https://showname.libsyn.com/rss`
   - Spreaker: `https://www.spreaker.com/show/{id}/episodes/feed`
   - Omny: Check page source for `omnycontent.com` URLs

## Usage

Add your modified feed URL to any podcast app:
```
http://your-server:8000/mypodcast
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | required | Claude API key |
| `BASE_URL` | `http://localhost:8000` | Public URL for generated feed links |
| `WHISPER_MODEL` | `small` | Whisper model size (tiny/base/small/medium/large) |
| `WHISPER_DEVICE` | `cuda` | Device for Whisper (cuda/cpu) |
