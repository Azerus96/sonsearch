import os
import asyncio
import aiohttp
from flask import Flask, render_template, request
from flask_sock import Sock
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import logging
import re
import time
from datetime import datetime
import json
from search_state import SearchProgress, search_states
import gc

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Константы
CONCURRENT_REQUESTS = 20  # Уменьшаем количество одновременных запросов
REQUEST_TIMEOUT = 30  # Увеличиваем таймаут
MAX_CONTENT_SIZE = 500_000
CHUNK_SIZE = 30
MAX_RETRIES = 3


# Настройки сессии
SESSION_SETTINGS = {
    'timeout': aiohttp.ClientTimeout(total=REQUEST_TIMEOUT, connect=10),
    'headers': {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
    },
    'ssl': False,  # Отключаем проверку SSL
}

app = Flask(__name__)
sock = Sock(app)

# Семафор для контроля параллельных запросов
semaphore = asyncio.Semaphore(CONCURRENT_REQUESTS)

# Хранилище состояний поиска
search_states = {}

class SearchProgress:
    def __init__(self, total_urls=0, processed_urls=0, found_results=0, 
                 current_status="waiting", start_time=0):
        self.total_urls = total_urls
        self.processed_urls = processed_urls
        self.found_results = found_results
        self.current_status = current_status
        self.start_time = start_time

    def to_json(self):
        return json.dumps({
            "total_urls": self.total_urls,
            "processed_urls": self.processed_urls,
            "found_results": self.found_results,
            "current_status": self.current_status,
            "progress": round((self.processed_urls / max(self.total_urls, 1)) * 100, 2),
            "elapsed_time": round(time.time() - self.start_time, 2) if self.start_time else 0
        })

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

async def fetch_page(session, url):
    """Асинхронное получение страницы"""
    async with semaphore:
        for retry in range(MAX_RETRIES):
            try:
                async with session.get(url, timeout=REQUEST_TIMEOUT, ssl=False) as response:
                    if response.status != 200:
                        logger.warning(f"Status {response.status} for {url}")
                        if retry == MAX_RETRIES - 1:
                            return None
                        continue

                    try:
                        content = ''
                        async for chunk in response.content.iter_chunked(8192):
                            try:
                                content += chunk.decode('utf-8', errors='ignore')
                                if len(content) > MAX_CONTENT_SIZE:
                                    return content[:MAX_CONTENT_SIZE]
                            except Exception as e:
                                logger.error(f"Error decoding chunk from {url}: {str(e)}")
                                continue
                        return content if content else None
                    except Exception as e:
                        logger.error(f"Error reading content from {url}: {str(e)}")
                        if retry == MAX_RETRIES - 1:
                            return None
                        continue

            except asyncio.TimeoutError:
                logger.warning(f"Timeout for {url} (attempt {retry + 1})")
            except aiohttp.ClientError as e:
                logger.error(f"Client error for {url} (attempt {retry + 1}): {str(e)}")
            except Exception as e:
                logger.error(f"Unexpected error for {url} (attempt {retry + 1}): {str(e)}")
            
            if retry < MAX_RETRIES - 1:
                await asyncio.sleep(1 * (retry + 1))  # Увеличиваем время ожидания с каждой попыткой
            else:
                return None
        return None

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
            gc.collect()
            
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

@app.route('/', methods=['GET', 'POST'])
async def index():
    """Основной обработчик"""
    try:
        if request.method == 'POST':
            start_url = request.form['url'].strip()
            search_term = request.form['search_term'].strip()
            max_depth = min(int(request.form['max_depth']), 10)
            max_pages = min(int(request.form['max_pages']), 100)

            if not is_valid_url(start_url):
                return render_template('index.html', error="Некорректный URL")

            # Создаем ID для поиска
            search_id = str(int(time.time()))
            search_states[search_id] = SearchProgress(start_time=time.time())

            try:
                connector = aiohttp.TCPConnector(
    limit=CONCURRENT_REQUESTS,
    force_close=True,
    enable_cleanup_closed=True,
    ssl=False
)

async with aiohttp.ClientSession(
    connector=connector,
    **SESSION_SETTINGS
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
                        gc.collect()

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

            except Exception as e:
                logger.error(f"Search error: {str(e)}")
                await update_search_progress(search_id, current_status="error")
                return render_template('index.html', error="Произошла ошибка при поиске")

        return render_template('index.html')

    except Exception as e:
        logger.error(f"An error occurred: {str(e)}")
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

@app.errorhandler(Exception)
def handle_exception(e):
    """Общий обработчик ошибок"""
    logger.error(f"Unhandled exception: {str(e)}")
    return render_template('index.html', error="Произошла непредвиденная ошибка")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)

