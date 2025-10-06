"""Ad detection using Claude API."""
import logging
import json
import os
from typing import List, Dict, Optional
from anthropic import Anthropic

logger = logging.getLogger(__name__)

class AdDetector:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get('ANTHROPIC_API_KEY')
        if not self.api_key:
            logger.warning("No Anthropic API key found")
        self.client = None

    def initialize_client(self):
        """Initialize Anthropic client."""
        if self.client is None and self.api_key:
            try:
                from anthropic import Anthropic
                self.client = Anthropic(api_key=self.api_key)
                logger.info("Anthropic client initialized")
            except Exception as e:
                logger.error(f"Failed to initialize Anthropic client: {e}")
                raise

    def detect_ads(self, segments: List[Dict], podcast_name: str = "Unknown", episode_title: str = "Unknown", slug: str = None, episode_id: str = None) -> Optional[List[Dict]]:
        """Detect ad segments using Claude API."""
        if not self.api_key:
            logger.warning("Skipping ad detection - no API key")
            return []

        try:
            self.initialize_client()

            # Prepare transcript with timestamps for Claude
            transcript_lines = []
            for segment in segments:
                start = segment['start']
                end = segment['end']
                text = segment['text']
                transcript_lines.append(f"[{start:.1f}s - {end:.1f}s] {text}")

            transcript = "\n".join(transcript_lines)

            # Call Claude API
            logger.info(f"Sending transcript to Claude for ad detection: {podcast_name} - {episode_title}")

            prompt = f"""Podcast: {podcast_name}
Episode: {episode_title}

Transcript:
{transcript}

INSTRUCTIONS:
Analyze this podcast transcript and identify ALL advertisement segments. Look for:
- Product endorsements, sponsored content, or promotional messages
- Promo codes, special offers, or calls to action
- Clear transitions to/from ads (e.g., "This episode is brought to you by...")
- Host-read advertisements
- Pre-roll, mid-roll, or post-roll ads
- Long intro sections filled with multiple ads before actual content begins
- Mentions of other podcasts/shows from the network (cross-promotion)
- Sponsor messages about credit cards, apps, products, or services
- ANY podcast promos (e.g., "Listen to X on iHeart Radio app")

CRITICAL MERGING RULES:
1. Analyze the FULL transcript before deciding segment boundaries - don't stop at gaps
2. Multiple ads separated by gaps of 15 seconds or less should be treated as ONE CONTINUOUS SEGMENT
3. Brief transitions, silence, or gaps between ads do NOT count as content - they're part of the same ad block
4. Only split ads if there's REAL SHOW CONTENT (actual discussion, interview, topic content) for at least 30 seconds between them
5. Consider the entire context: if ads at 1500s, 1520s, 1540s are all promotional content, return ONE segment from 1500-1560, not three separate ones
6. When in doubt, merge the segments - better to remove too much than leave ads in
7. If there's a gap followed by content that doesn't continue the previous discussion but instead introduces a completely new topic/person/show, it's likely still part of the ad block

Return ONLY a JSON array of ad segments with start/end times in seconds. Be aggressive in detecting ads.

Format:
[{{"start": 0.0, "end": 240.0, "reason": "Continuous ad block: multiple sponsors"}}, ...]

If no ads are found, return an empty array: []"""

            # Save the prompt for debugging
            if slug and episode_id:
                try:
                    from storage import Storage
                    storage = Storage()
                    storage.save_prompt(slug, episode_id, prompt)
                except Exception as e:
                    logger.warning(f"Could not save prompt: {e}")

            response = self.client.messages.create(
                model="claude-opus-4-1-20250805",  # Use Claude Opus 4.1 for better ad detection
                max_tokens=2000,
                temperature=0.2,
                system="You are an ad detection specialist with extensive experience in identifying all forms of advertisements, sponsorships, and promotional content in podcasts. Your users absolutely cannot tolerate ads - they find them disruptive and want them completely removed. Be extremely aggressive in detecting ads. When in doubt, mark it as an ad. It's better to remove a few seconds of content than to leave any advertisement in the podcast.",
                messages=[{
                    "role": "user",
                    "content": prompt
                }]
            )

            # Extract JSON from response
            response_text = response.content[0].text if response.content else ""
            logger.info(f"Claude response received: {len(response_text)} chars")

            # Try to parse JSON from response
            try:
                # Look for JSON array in response
                start_idx = response_text.find('[')
                end_idx = response_text.rfind(']') + 1
                if start_idx >= 0 and end_idx > start_idx:
                    json_str = response_text[start_idx:end_idx]
                    ads = json.loads(json_str)

                    # Validate structure
                    if isinstance(ads, list):
                        valid_ads = []
                        for ad in ads:
                            if isinstance(ad, dict) and 'start' in ad and 'end' in ad:
                                valid_ads.append({
                                    'start': float(ad['start']),
                                    'end': float(ad['end']),
                                    'reason': ad.get('reason', 'Advertisement detected')
                                })

                        total_ad_time = sum(ad['end'] - ad['start'] for ad in valid_ads)
                        logger.info(f"Claude detected {len(valid_ads)} ad segments (total {total_ad_time/60:.1f} minutes)")

                        # Store full response for debugging
                        return {
                            "ads": valid_ads,
                            "raw_response": response_text,
                            "model": "claude-sonnet-4-5-20250929"
                        }
                else:
                    logger.warning("No JSON array found in Claude response")
                    return {"ads": [], "raw_response": response_text, "error": "No JSON found"}

            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse JSON from Claude response: {e}")
                return {"ads": [], "raw_response": response_text, "error": str(e)}

        except Exception as e:
            logger.error(f"Ad detection failed: {e}")
            return {"ads": [], "error": str(e)}

    def process_transcript(self, segments: List[Dict], podcast_name: str = "Unknown", episode_title: str = "Unknown", slug: str = None, episode_id: str = None) -> Dict:
        """Process transcript for ad detection."""
        result = self.detect_ads(segments, podcast_name, episode_title, slug, episode_id)
        if result is None:
            return {"ads": [], "error": "Detection failed"}
        return result