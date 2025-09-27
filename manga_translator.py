#!/usr/bin/env python3
"""
Automated Manga Translation System
Translates manga from Japanese/Korean to Marathi
"""

import os
import sys
import json
import time
import random
import logging
from datetime import datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup
import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
import easyocr
from googletrans import Translator
import sqlite3
from typing import List, Dict, Tuple, Optional

# Configuration
CONFIG = {
    'FREQUENCY_HOURS': 24,  # Daily by default
    'MAX_CHAPTERS_PER_DAY': 1,
    'OUTPUT_DIR': 'output',
    'TEMP_DIR': 'temp',
    'DATABASE': 'manga_db.sqlite',
    'USER_AGENT': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'BLOGGER_BLOG_ID': os.getenv('BLOGGER_BLOG_ID', ''),
    'BLOGGER_API_KEY': os.getenv('BLOGGER_API_KEY', ''),
    'SITES': [
        'https://bato.to',
        'https://mangadex.org'
    ]
}

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('manga_translator.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class DatabaseManager:
    """Manages SQLite database for manga tracking and voting"""
    
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.init_database()
    
    def init_database(self):
        """Initialize database tables"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Manga series table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS manga_series (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT UNIQUE NOT NULL,
                source_url TEXT,
                language TEXT,
                status TEXT DEFAULT 'active',
                last_chapter INTEGER DEFAULT 0,
                votes INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Chapters table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS chapters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                manga_id INTEGER,
                chapter_number INTEGER,
                chapter_url TEXT,
                translated_at TIMESTAMP,
                blogger_post_id TEXT,
                FOREIGN KEY (manga_id) REFERENCES manga_series (id)
            )
        ''')
        
        # Voting table (simple IP-based voting)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS votes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                manga_id INTEGER,
                voter_ip TEXT,
                voted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (manga_id) REFERENCES manga_series (id),
                UNIQUE(manga_id, voter_ip)
            )
        ''')
        
        conn.commit()
        conn.close()
    
    def get_next_manga_to_translate(self) -> Optional[Dict]:
        """Get the next manga to translate based on votes and schedule"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT id, title, source_url, last_chapter, votes
            FROM manga_series 
            WHERE status = 'active'
            ORDER BY votes DESC, created_at ASC
            LIMIT 1
        ''')
        
        result = cursor.fetchone()
        conn.close()
        
        if result:
            return {
                'id': result[0],
                'title': result[1],
                'source_url': result[2],
                'last_chapter': result[3],
                'votes': result[4]
            }
        return None
    
    def add_vote(self, manga_id: int, voter_ip: str) -> bool:
        """Add a vote for a manga"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                'INSERT INTO votes (manga_id, voter_ip) VALUES (?, ?)',
                (manga_id, voter_ip)
            )
            # Update vote count
            cursor.execute(
                'UPDATE manga_series SET votes = votes + 1 WHERE id = ?',
                (manga_id,)
            )
            conn.commit()
            conn.close()
            return True
        except sqlite3.IntegrityError:
            return False  # Already voted

class MangaScraper:
    """Scrapes manga from various sources"""
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': CONFIG['USER_AGENT']})
        
    def get_chapter_images(self, manga_url: str, chapter_num: int) -> List[str]:
        """Get list of image URLs for a chapter"""
        try:
            # Try MangaDex first
            if 'mangadex.org' in manga_url:
                return self._get_mangadex_images(manga_url, chapter_num)
            else:
                # Generic scraper for other sites
                return self._get_generic_images(manga_url, chapter_num)
        except Exception as e:
            logger.error(f"Error scraping {manga_url}: {e}")
            return []
    
    def _get_mangadex_images(self, manga_url: str, chapter_num: int) -> List[str]:
        """Specific scraper for MangaDex"""
        # MangaDex has an API
        manga_id = manga_url.split('/')[-1]
        
        # Get chapter list
        chapters_api = f"https://api.mangadex.org/manga/{manga_id}/feed"
        params = {
            'chapter': str(chapter_num),
            'translatedLanguage[]': ['ja', 'ko']  # Japanese or Korean
        }
        
        response = self.session.get(chapters_api, params=params)
        if response.status_code != 200:
            return []
            
        data = response.json()
        if not data['data']:
            return []
            
        chapter_id = data['data'][0]['id']
        
        # Get chapter images
        chapter_api = f"https://api.mangadex.org/at-home/server/{chapter_id}"
        response = self.session.get(chapter_api)
        if response.status_code != 200:
            return []
            
        chapter_data = response.json()
        base_url = chapter_data['baseUrl']
        chapter_hash = chapter_data['chapter']['hash']
        
        images = []
        for filename in chapter_data['chapter']['data']:
            image_url = f"{base_url}/data/{chapter_hash}/{filename}"
            images.append(image_url)
            
        return images
    
    def _get_generic_images(self, manga_url: str, chapter_num: int) -> List[str]:
        """Generic scraper for other manga sites"""
        try:
            # This is a basic implementation - would need site-specific logic
            chapter_url = f"{manga_url}/chapter-{chapter_num}"
            response = self.session.get(chapter_url)
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Common selectors for manga images
            selectors = [
                'img[src*="cdn"]',
                '.chapter-img img',
                '.reader-img',
                'img[data-src]'
            ]
            
            images = []
            for selector in selectors:
                img_tags = soup.select(selector)
                for img in img_tags:
                    src = img.get('src') or img.get('data-src')
                    if src and any(ext in src.lower() for ext in ['.jpg', '.png', '.jpeg', '.webp']):
                        if src.startswith('//'):
                            src = 'https:' + src
                        elif src.startswith('/'):
                            src = manga_url + src
                        images.append(src)
                        
            return list(set(images))  # Remove duplicates
            
        except Exception as e:
            logger.error(f"Generic scraping failed: {e}")
            return []

class ImageProcessor:
    """Handles OCR and text overlay"""
    
    def __init__(self):
        self.ocr_reader = easyocr.Reader(['ja', 'ko'], gpu=False)
        self.translator = Translator()
        
        # Try to load Marathi font
        self.marathi_font = self._load_marathi_font()
    
    def _load_marathi_font(self) -> Optional[ImageFont.FreeTypeFont]:
        """Load Marathi font for text rendering"""
        font_paths = [
            '/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf',
            '/System/Library/Fonts/Arial.ttf',
            'arial.ttf'
        ]
        
        for font_path in font_paths:
            try:
                return ImageFont.truetype(font_path, size=20)
            except:
                continue
        
        # Fallback to default
        try:
            return ImageFont.load_default()
        except:
            return None
    
    def process_image(self, image_path: str, output_path: str) -> bool:
        """Process a single manga page - OCR and translate"""
        try:
            # Load image
            img = cv2.imread(image_path)
            if img is None:
                return False
                
            # OCR to detect text
            results = self.ocr_reader.readtext(img)
            
            # Convert to PIL for text rendering
            pil_img = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
            draw = ImageDraw.Draw(pil_img)
            
            for (bbox, text, confidence) in results:
                if confidence > 0.5:  # Only process confident detections
                    # Translate text
                    translated_text = self._translate_text(text)
                    if translated_text:
                        # Get bounding box
                        (top_left, top_right, bottom_right, bottom_left) = bbox
                        x = int(top_left[0])
                        y = int(top_left[1])
                        width = int(top_right[0] - top_left[0])
                        height = int(bottom_left[1] - top_left[1])
                        
                        # Clear original text area (white rectangle)
                        draw.rectangle([x, y, x + width, y + height], fill='white')
                        
                        # Add translated text
                        self._draw_text_with_wrap(draw, translated_text, x, y, width, height)
            
            # Save processed image
            pil_img.save(output_path, 'JPEG', quality=95)
            return True
            
        except Exception as e:
            logger.error(f"Error processing image {image_path}: {e}")
            return False
    
    def _translate_text(self, text: str) -> Optional[str]:
        """Translate Japanese/Korean text to Marathi"""
        try:
            # First translate to English, then to Marathi for better quality
            english_translation = self.translator.translate(text, dest='en').text
            marathi_translation = self.translator.translate(english_translation, dest='mr').text
            return marathi_translation
        except Exception as e:
            logger.error(f"Translation error: {e}")
            return None
    
    def _draw_text_with_wrap(self, draw, text: str, x: int, y: int, width: int, height: int):
        """Draw text with word wrapping"""
        if not self.marathi_font:
            return
            
        words = text.split()
        lines = []
        current_line = []
        
        for word in words:
            test_line = ' '.join(current_line + [word])
            bbox = draw.textbbox((0, 0), test_line, font=self.marathi_font)
            if bbox[2] - bbox[0] <= width - 10:  # 10px margin
                current_line.append(word)
            else:
                if current_line:
                    lines.append(' '.join(current_line))
                current_line = [word]
        
        if current_line:
            lines.append(' '.join(current_line))
        
        # Draw lines
        line_height = 25
        for i, line in enumerate(lines[:int(height/line_height)]):  # Fit in available height
            draw.text((x + 5, y + 5 + i * line_height), line, 
                     fill='black', font=self.marathi_font)

class BloggerUploader:
    """Handles uploading to Blogger"""
    
    def __init__(self):
        self.blog_id = CONFIG['BLOGGER_BLOG_ID']
        self.api_key = CONFIG['BLOGGER_API_KEY']
        self.base_url = 'https://www.googleapis.com/blogger/v3'
    
    def upload_chapter(self, manga_title: str, chapter_num: int, image_paths: List[str]) -> Optional[str]:
        """Upload a chapter to Blogger"""
        if not self.blog_id or not self.api_key:
            logger.warning("Blogger credentials not configured")
            return None
            
        try:
            # Create HTML content with images
            html_content = self._create_post_html(manga_title, chapter_num, image_paths)
            
            # Create post
            post_data = {
                'kind': 'blogger#post',
                'title': f'{manga_title} - Chapter {chapter_num}',
                'content': html_content,
                'labels': ['manga', 'translation', 'marathi']
            }
            
            url = f'{self.base_url}/blogs/{self.blog_id}/posts'
            params = {'key': self.api_key}
            
            response = requests.post(url, json=post_data, params=params)
            
            if response.status_code == 200:
                post_id = response.json()['id']
                logger.info(f"Posted to Blogger: {post_id}")
                return post_id
            else:
                logger.error(f"Blogger upload failed: {response.text}")
                return None
                
        except Exception as e:
            logger.error(f"Blogger upload error: {e}")
            return None
    
    def _create_post_html(self, manga_title: str, chapter_num: int, image_paths: List[str]) -> str:
        """Create HTML content for the blog post"""
        # Get current voting data
        voting_html = self._generate_voting_section()
        
        html = f'''
        <div class="manga-chapter">
            <h2>{manga_title} - Chapter {chapter_num}</h2>
            <p>🌐 Translated to Marathi | 📅 Published: {datetime.now().strftime('%B %d, %Y')}</p>
            <div class="chapter-images" style="margin: 20px 0;">
        '''
        
        for i, img_path in enumerate(image_paths):
            # In a real implementation, you'd upload images to a CDN or image hosting service
            # For now, we'll use placeholder - you'll need to implement image upload
            html += f'''<img src="data:image/jpeg;base64,placeholder" 
                       alt="Page {i+1}" 
                       style="width:100%; max-width:800px; margin:10px auto; display:block; border-radius:8px; box-shadow: 0 4px 8px rgba(0,0,0,0.1);"><br>'''
        
        html += f'''
            </div>
            <hr style="margin: 30px 0; border: none; border-top: 2px solid #eee;">
            {voting_html}
        </div>
        '''
        
        return html
    
    def _generate_voting_section(self) -> str:
        """Generate embedded voting section HTML"""
        # Get manga data from database
        conn = sqlite3.connect(CONFIG['DATABASE'])
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, title, votes, status, last_chapter
            FROM manga_series 
            WHERE status = 'active'
            ORDER BY votes DESC
            LIMIT 8
        ''')
        manga_list = cursor.fetchall()
        conn.close()
        
        voting_html = '''
        <style>
        .voting-section {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            border-radius: 15px;
            padding: 25px;
            color: white;
            margin: 20px 0;
        }
        .voting-title {
            text-align: center;
            font-size: 1.5em;
            margin-bottom: 20px;
            color: white;
        }
        .manga-vote-item {
            display: flex;
            justify-content: space-between;
            align-items: center;
            background: rgba(255,255,255,0.1);
            margin: 8px 0;
            padding: 12px 15px;
            border-radius: 8px;
            transition: all 0.3s ease;
        }
        .manga-vote-item:hover {
            background: rgba(255,255,255,0.2);
            transform: translateX(5px);
        }
        .manga-vote-info {
            flex-grow: 1;
        }
        .manga-vote-title {
            font-weight: bold;
            font-size: 1.1em;
        }
        .manga-vote-details {
            font-size: 0.9em;
            opacity: 0.8;
        }
        .vote-count {
            background: rgba(255,255,255,0.2);
            padding: 5px 12px;
            border-radius: 20px;
            font-weight: bold;
            margin-right: 10px;
        }
        .vote-btn {
            background: #28a745;
            color: white;
            border: none;
            padding: 8px 15px;
            border-radius: 20px;
            cursor: pointer;
            font-weight: bold;
            transition: all 0.3s ease;
        }
        .vote-btn:hover {
            background: #218838;
            transform: scale(1.05);
        }
        .poll-footer {
            text-align: center;
            margin-top: 15px;
            opacity: 0.8;
            font-size: 0.9em;
        }
        @media (max-width: 768px) {
            .manga-vote-item {
                flex-direction: column;
                text-align: center;
                gap: 10px;
            }
        }
        </style>
        
        <div class="voting-section">
            <div class="voting-title">🗳️ Vote for the Next Manga Translation!</div>
        '''
        
        for manga in manga_list[:5]:  # Show top 5 manga
            manga_id, title, votes, status, last_chapter = manga
            voting_html += f'''
            <div class="manga-vote-item">
                <div class="manga-vote-info">
                    <div class="manga-vote-title">{title}</div>
                    <div class="manga-vote-details">{status} • Chapter {last_chapter}</div>
                </div>
                <div style="display: flex; align-items: center;">
                    <div class="vote-count">{votes}</div>
                    <button class="vote-btn" onclick="voteForManga({manga_id}, '{title}')">Vote</button>
                </div>
            </div>
            '''
        
        voting_html += '''
            <div class="poll-footer">
                🎌 Help us choose which manga to translate next! Your vote matters.
            </div>
        </div>
        
        <script>
        function voteForManga(mangaId, title) {
            // Check if user already voted
            const voted = localStorage.getItem('manga_votes_' + mangaId);
            if (voted) {
                alert('आपण या मांगासाठी आधीच मत दिले आहे! (You have already voted for this manga!)');
                return;
            }
            
            // Simulate vote (in real implementation, this would call your API)
            fetch('https://your-api-endpoint.com/vote', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    manga_id: mangaId,
                    voter_ip: 'browser_fingerprint'  // Better identification needed
                })
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    // Mark as voted
                    localStorage.setItem('manga_votes_' + mangaId, 'true');
                    
                    // Update UI
                    const btn = event.target;
                    btn.innerText = '✓ Voted';
                    btn.disabled = true;
                    btn.style.background = '#6c757d';
                    
                    // Update vote count
                    const countEl = btn.previousElementSibling;
                    countEl.innerText = parseInt(countEl.innerText) + 1;
                    
                    alert('धन्यवाद! तुमचे मत नोंदवले गेले. (Thank you! Your vote has been recorded.)');
                } else {
                    alert('Error: Could not register vote. Please try again later.');
                }
            })
            .catch(error => {
                console.error('Voting error:', error);
                alert('Network error. Please check your connection and try again.');
            });
        }
        
        // Mark already voted items on page load
        document.addEventListener('DOMContentLoaded', function() {
            const buttons = document.querySelectorAll('.vote-btn');
            buttons.forEach(btn => {
                const mangaId = btn.getAttribute('onclick').match(/\\d+/)[0];
                if (localStorage.getItem('manga_votes_' + mangaId)) {
                    btn.innerText = '✓ Voted';
                    btn.disabled = true;
                    btn.style.background = '#6c757d';
                }
            });
        });
        </script>
        '''
        
        return voting_html

class MangaTranslationSystem:
    """Main system orchestrator"""
    
    def __init__(self):
        self.db = DatabaseManager(CONFIG['DATABASE'])
        self.scraper = MangaScraper()
        self.processor = ImageProcessor()
        self.uploader = BloggerUploader()
        
        # Create directories
        Path(CONFIG['OUTPUT_DIR']).mkdir(exist_ok=True)
        Path(CONFIG['TEMP_DIR']).mkdir(exist_ok=True)
    
    def run_daily_translation(self):
        """Main function to run daily translation"""
        logger.info("Starting daily translation process")
        
        # Get next manga to translate
        manga = self.db.get_next_manga_to_translate()
        if not manga:
            logger.warning("No manga found to translate")
            return
        
        logger.info(f"Processing: {manga['title']} - Chapter {manga['last_chapter'] + 1}")
        
        # Get chapter images
        next_chapter = manga['last_chapter'] + 1
        image_urls = self.scraper.get_chapter_images(manga['source_url'], next_chapter)
        
        if not image_urls:
            logger.error(f"No images found for chapter {next_chapter}")
            return
        
        # Download and process images
        processed_images = []
        
        for i, img_url in enumerate(image_urls):
            try:
                # Download image
                response = requests.get(img_url, stream=True)
                if response.status_code == 200:
                    input_path = Path(CONFIG['TEMP_DIR']) / f"page_{i:03d}.jpg"
                    output_path = Path(CONFIG['OUTPUT_DIR']) / f"{manga['title']}_ch{next_chapter}_page_{i:03d}.jpg"
                    
                    # Save original
                    with open(input_path, 'wb') as f:
                        for chunk in response.iter_content(chunk_size=8192):
                            f.write(chunk)
                    
                    # Process with OCR and translation
                    if self.processor.process_image(str(input_path), str(output_path)):
                        processed_images.append(str(output_path))
                        logger.info(f"Processed page {i+1}/{len(image_urls)}")
                    
                    # Cleanup temp file
                    input_path.unlink(missing_ok=True)
                    
            except Exception as e:
                logger.error(f"Error processing image {i}: {e}")
        
        # Upload to Blogger
        if processed_images:
            post_id = self.uploader.upload_chapter(
                manga['title'], 
                next_chapter, 
                processed_images
            )
            
            if post_id:
                # Update database
                conn = sqlite3.connect(CONFIG['DATABASE'])
                cursor = conn.cursor()
                cursor.execute(
                    'UPDATE manga_series SET last_chapter = ? WHERE id = ?',
                    (next_chapter, manga['id'])
                )
                cursor.execute(
                    'INSERT INTO chapters (manga_id, chapter_number, blogger_post_id) VALUES (?, ?, ?)',
                    (manga['id'], next_chapter, post_id)
                )
                conn.commit()
                conn.close()
                
                logger.info(f"Successfully translated and uploaded chapter {next_chapter}")
            else:
                logger.error("Failed to upload to Blogger")
        else:
            logger.error("No images were successfully processed")
    
    def add_manga_series(self, title: str, source_url: str, language: str = 'ja'):
        """Add a new manga series to track"""
        conn = sqlite3.connect(CONFIG['DATABASE'])
        cursor = conn.cursor()
        try:
            cursor.execute(
                'INSERT INTO manga_series (title, source_url, language) VALUES (?, ?, ?)',
                (title, source_url, language)
            )
            conn.commit()
            logger.info(f"Added manga series: {title}")
        except sqlite3.IntegrityError:
            logger.warning(f"Manga series already exists: {title}")
        finally:
            conn.close()

def main():
    """Main entry point"""
    system = MangaTranslationSystem()
    
    # Add some sample manga series (you can modify this)
    sample_series = [
        ('One Piece', 'https://mangadex.org/title/a1c7c817-4e59-43b7-9365-09675a149a6f', 'ja'),
        ('Solo Leveling', 'https://mangadex.org/title/32d76d19-8a05-4db0-9fc2-e0b0648fe9d0', 'ko'),
    ]
    
    for title, url, lang in sample_series:
        system.add_manga_series(title, url, lang)
    
    # Run translation
    system.run_daily_translation()

if __name__ == "__main__":
    main()
