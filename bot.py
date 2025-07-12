import asyncio
import logging
import threading
import sqlite3
import time
import feedparser
from bs4 import BeautifulSoup
import cloudscraper
from flask import Flask
from pyrogram import Client, utils as pyroutils, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from config import BOT, API, OWNER, CHANNEL, RSS_FEEDS

# Ensure proper chat/channel ID handling
pyroutils.MIN_CHAT_ID = -999999999999
pyroutils.MIN_CHANNEL_ID = -10099999999999

# Logging configuration
logging.getLogger().setLevel(logging.INFO)
logging.getLogger("pyrogram").setLevel(logging.ERROR)

# Flask health check
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running!"

def run_flask():
    app.run(host='0.0.0.0', port=8000)

# Database functions for news tracking
def init_db():
    conn = sqlite3.connect("news.db")
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS sent_news
                 (link TEXT PRIMARY KEY, timestamp INTEGER)''')
    conn.commit()
    conn.close()

def is_news_sent(link):
    conn = sqlite3.connect("news.db")
    c = conn.cursor()
    c.execute("SELECT 1 FROM sent_news WHERE link=?", (link,))
    exists = c.fetchone() is not None
    conn.close()
    return exists

def mark_as_sent(link):
    conn = sqlite3.connect("news.db")
    c = conn.cursor()
    c.execute("INSERT INTO sent_news VALUES (?, ?)", (link, int(time.time())))
    conn.commit()
    conn.close()

# News processing functions
def split_text(text, chunk_size=4000):
    return [text[i:i+chunk_size] for i in range(0, len(text), chunk_size)]

def create_nav_buttons(current_page, total_pages, news_id):
    buttons = []
    if total_pages > 1:
        if current_page > 1:
            buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"nav_{news_id}_{current_page-1}"))
        buttons.append(InlineKeyboardButton(f"{current_page}/{total_pages}", callback_data="noop"))
        if current_page < total_pages:
            buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"nav_{news_id}_{current_page+1}"))
    return InlineKeyboardMarkup([buttons]) if buttons else None

async def get_full_article_text(url):
    try:
        scraper = cloudscraper.create_scraper()
        response = scraper.get(url, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Extract main article content - adjust selectors as needed
        article_body = soup.find('div', class_='article-body')
        if not article_body:
            return "Could not retrieve full article content."
        
        # Remove unwanted elements
        for unwanted in article_body.find_all(['script', 'style', 'div', class_='advtBlock']):
            unwanted.decompose()
        
        # Get clean text paragraphs
        paragraphs = [p.get_text().strip() for p in article_body.find_all('p')]
        return "\n\n".join([p for p in paragraphs if p])
    
    except Exception as e:
        logging.error(f"Error fetching article: {e}")
        return "Could not retrieve full article content."

async def format_news(entry):
    message = f"**{entry.title}**\n\n"
    if hasattr(entry, 'description'):
        message += f"{entry.description}\n\n"
    
    full_text = await get_full_article_text(entry.link)
    message += full_text
    
    if hasattr(entry, 'published'):
        message += f"\n\n📅 Published: {entry.published}"
    
    message += f"\n\n[Read original article]({entry.link})"
    return message

class NewsBot(Client):
    MAX_MSG_LENGTH = 4000

    def __init__(self):
        super().__init__(
            "NewsBot",
            api_id=API.ID,
            api_hash=API.HASH,
            bot_token=BOT.TOKEN,
            workers=4
        )
        self.channel_id = CHANNEL.ID
        self.news_cache = {}  # Stores news articles for pagination

    async def safe_send_message(self, chat_id, text, **kwargs):
        for chunk in (text[i:i+self.MAX_MSG_LENGTH] for i in range(0, len(text), self.MAX_MSG_LENGTH)):
            await self.send_message(chat_id, chunk, **kwargs)
            await asyncio.sleep(1)

    async def auto_post_news(self):
        while True:
            try:
                for feed_url in RSS_FEEDS:
                    try:
                        feed = feedparser.parse(feed_url)
                        for entry in feed.entries[:5]:  # Process latest 5 items
                            if not is_news_sent(entry.link):
                                formatted_message = await format_news(entry)
                                message_chunks = split_text(formatted_message)
                                
                                # Create unique ID for this news item
                                news_id = hash(entry.link) % (10**8)
                                self.news_cache[news_id] = formatted_message
                                
                                # Send first chunk with navigation if needed
                                keyboard = create_nav_buttons(1, len(message_chunks), news_id)
                                await self.send_message(
                                    self.channel_id,
                                    text=message_chunks[0],
                                    reply_markup=keyboard,
                                    disable_web_page_preview=True,
                                    parse_mode=enums.ParseMode.MARKDOWN
                                )
                                
                                mark_as_sent(entry.link)
                                await asyncio.sleep(5)  # Avoid rate limiting
                    except Exception as e:
                        logging.error(f"Error processing {feed_url}: {e}")
                
                await asyncio.sleep(1800)  # Check every 30 minutes
            except Exception as e:
                logging.error(f"Error in auto_post_news: {e}")
                await asyncio.sleep(300)  # Wait 5 minutes before retrying

    async def handle_navigation(self, client, query):
        _, news_id, page = query.data.split("_")
        news_id = int(news_id)
        page = int(page)
        
        if news_id in self.news_cache:
            full_text = self.news_cache[news_id]
            chunks = split_text(full_text)
            total_pages = len(chunks)
            
            if 1 <= page <= total_pages:
                keyboard = create_nav_buttons(page, total_pages, news_id)
                await query.message.edit_text(
                    text=chunks[page-1],
                    reply_markup=keyboard,
                    disable_web_page_preview=True
                )
        await query.answer()

    async def start(self):
        await super().start()
        init_db()
        me = await self.get_me()
        BOT.USERNAME = f"@{me.username}"
        
        # Register callback handler for pagination
        self.add_handler(filters.callback_query(filters.regex("^nav_")), self.handle_navigation)
        
        await self.send_message(
            OWNER.ID,
            text=f"{me.first_name} ✅ News bot started with RSS support"
        )
        logging.info("News bot started with RSS support")
        
        # Start the news posting task
        asyncio.create_task(self.auto_post_news())

    async def stop(self, *args):
        await super().stop()
        logging.info("News bot stopped")

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    NewsBot().run()
