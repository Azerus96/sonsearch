import os
import asyncio
import aiohttp
from flask import Flask, render_template, request, make_response, send_from_directory
from flask_sock import Sock
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import logging
import re
import time
from datetime import datetime
import json
from search_state import SearchProgress, search_states
from functools import lru_cache
import gc

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Константы
CONCURRENT_REQUESTS = 30
REQUEST_TIMEOUT = 15
MAX_CONTENT_SIZE = 500_000
CHUNK_SIZE = 50

app = Flask(__name__)
sock = Sock(app)

class SearchResult:
    def __init__(self, context, title, url, count):
        self.context = context
        self.title = title
        self.url = url
        self.count = count

async def update_search_progress(search_id, **kwargs):
    """Обновление прогресса поиска"""
    if search_id in search_states:
        for key, value in kwargs.items():
            setattr(search_states[search_id], key, value)

@lru_cache(maxsize=100)
async def fetch_page(session, url):
    """Асинхронное получение страницы с кэшированием"""
    try:
        async with session.get(url, timeout=REQUEST_TIMEOUT) as response:
            if response.status != 200:
                return None
            content = ''
            async for chunk in response.content.iter_chunked(8192):
                content += chunk.decode('utf-8', errors='ignore')
                if len(content) > MAX_CONTENT_SIZE:
                    return content[:MAX_CONTENT_SIZE]
            return content
    except Exception as e:
        logger.error(f"Error fetching {url}: {str(e)}")
        return None
    finally:
        gc.collect()

def is_valid_url(url):
    """Проверка валидности URL"""
    try:
        result = urlparse(url)
        return all([result.scheme, result.netloc])
    except:
        return False

def extract_text_with_context(html, search_term):
    """Извлечение текста с контекстом"""
    try:
        soup = BeautifulSoup(html, 'lxml')
        for tag in soup(['script', 'style', 'meta', 'link', 'noscript', 'header', 'footer']):
            tag.decompose()
        
        text_blocks = []
        for paragraph in soup.find_all(['p', 'div', 'h1', 'h2', 'h3', 'h4', 'h5']):
            text_blocks.append(paragraph.get_text(strip=True))
        
        text = ' '.join(text_blocks)
        
        contexts = []
        pattern = f'.{{0,50}}{re.escape(search_term)}.{{0,50}}'
        matches = re.finditer(pattern, text, re.IGNORECASE)
        
        seen = set()
        for match in matches:
            context = match.group().strip()
            if context not in seen:
                contexts.append(context)
                seen.add(context)
                if len(contexts) >= 3:
                    break
        
        return contexts
    except Exception as e:
        logger.error(f"Error extracting text: {str(e)}")
        return []

async def search_page(session, url, search_term):
    """Асинхронный поиск на странице"""
    try:
        content = await fetch_page(session, url)
        if not content:
            return None

        soup = BeautifulSoup(content, 'lxml')
        title = soup.title.string if soup.title else url

        contexts = extract_text_with_context(content, search_term)
        if not contexts:
            return None

        return SearchResult(
            context=contexts[0],
            title=title.strip(),
            url=url,
            count=len(contexts)
        )
    except Exception as e:
        logger.error(f"Error searching page {url}: {str(e)}")
        return None

async def get_links(session, start_url, max_depth):
    """Асинхронное получение списка ссылок"""
    try:
        visited = set()
        to_visit = {start_url}
        base_domain = urlparse(start_url).netloc
        
        for depth in range(max_depth):
            current_urls = list(to_visit - visited)[:CHUNK_SIZE]
            if not current_urls:
                break
                
            tasks = [fetch_page(session, url) for url in current_urls]
            responses = await asyncio.gather(*tasks)
            
            to_visit.difference_update(current_urls)
            visited.update(current_urls)
            
            for url, content in zip(current_urls, responses):
                if not content:
                    continue
                    
                try:
                    soup = BeautifulSoup(content, 'lxml')
                    for link in soup.find_all('a', href=True):
                        href = link.get('href')
                        if not href:
                            continue
                            
                        full_url = urljoin(url, href)
                        if (urlparse(full_url).netloc == base_domain and 
                            full_url not in visited and 
                            len(visited) < max_depth):
                            to_visit.add(full_url)
                    
                    del soup
                    del content
                except Exception as e:
                    logger.error(f"Error parsing links from {url}: {str(e)}")
                    
            if len(visited) >= max_depth:
                break
                
            await asyncio.sleep(0.1)
            
        return list(visited)
    except Exception as e:
        logger.error(f"Error in get_links: {str(e)}")
        return []

@sock.route('/ws/<search_id>')
def websocket_endpoint(sock, search_id):
    """WebSocket endpoint для обновлений прогресса"""
    if search_id not in search_states:
        return
    
    while True:
        try:
            data = search_states[search_id].to_json()
            sock.send(data)
            if search_states[search_id].current_status in ['completed', 'error']:
                break
            time.sleep(0.5)
        except Exception as e:
            logger.error(f"WebSocket error: {str(e)}")
            break

@app.route('/favicon.ico')
def favicon():
    """Обработчик favicon.ico"""
    return send_from_directory(
        os.path.join(app.root_path, 'static'),
        'favicon.ico',
        mimetype='image/vnd.microsoft.icon'
    )

@app.route('/', methods=['GET', 'POST'])
async def index():
    """Основной обработчик"""
    try:
        if request.method == 'POST':
            start_url = request.form['url'].strip()
            search_term = request.form['search_term'].strip()
            max_depth = min(int(request.form['max_depth']), 10)
            max_pages = min(int(request.form['max_pages']), 100)

            # Создаем ID для поиска
            search_id = str(int(time.time()))
            search_states[search_id] = SearchProgress(start_time=time.time())

            async with aiohttp.ClientSession(
                headers={'User-Agent': 'Mozilla/5.0'},
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                connector=aiohttp.TCPConnector(limit=CONCURRENT_REQUESTS)
            ) as session:
                # Получение ссылок
                await update_search_progress(search_id, current_status="collecting_urls")
                all_urls = await get_links(session, start_url, max_pages)
                
                await update_search_progress(
                    search_id,
                    total_urls=len(all_urls),
                    current_status="searching"
                )

                # Поиск по страницам
                results = []
                for i in range(0, len(all_urls), CHUNK_SIZE):
                    chunk = all_urls[i:i + CHUNK_SIZE]
                    tasks = [search_page(session, url, search_term) for url in chunk]
                    chunk_results = await asyncio.gather(*tasks)
                    
                    valid_results = [r for r in chunk_results if r]
                    results.extend(valid_results)
                    
                    await update_search_progress(
                        search_id,
                        processed_urls=i + len(chunk),
                        found_results=len(results)
                    )
                    
                    # Очистка памяти
                    del chunk_results
                    await asyncio.sleep(0.1)

                # Сортировка и финальное обновление
                results.sort(key=lambda x: x.count, reverse=True)
                await update_search_progress(
                    search_id,
                    current_status="completed",
                    processed_urls=len(all_urls)
                )

                return render_template(
                    'index.html',
                    results=results[:50],
                    url=start_url,
                    search_term=search_term,
                    search_id=search_id
                )

        return render_template('index.html')

    except Exception as e:
        logger.error(f"An error occurred: {str(e)}")
        if 'search_id' in locals():
            await update_search_progress(search_id, current_status="error")
        return render_template('index.html', error="Произошла ошибка при выполнении поиска")

@app.before_request
def cleanup_old_states():
    """Очистка старых состояний поиска"""
    current_time = time.time()
    to_remove = [
        search_id for search_id, state in search_states.items()
        if current_time - state.start_time > 3600  # Удаляем через час
    ]
    for search_id in to_remove:
        del search_states[search_id]

@app.errorhandler(429)
def ratelimit_handler(e):
    """Обработчик превышения лимита запросов"""
    return render_template('index.html', error="Слишком много запросов. Пожалуйста, подождите.")

@app.errorhandler(Exception)
def handle_exception(e):
    """Общий обработчик ошибок"""
    logger.error(f"Unhandled exception: {str(e)}")
    return render_template('index.html', error="Произошла непредвиденная ошибка")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
