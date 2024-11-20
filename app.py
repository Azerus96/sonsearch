import os
from flask import Flask, render_template, request, make_response, send_from_directory
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import concurrent.futures
import re
import logging
from functools import wraps, lru_cache
from time import time
from datetime import datetime, timedelta
import threading
import gc
from apscheduler.schedulers.background import BackgroundScheduler

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Константы
REQUESTS_TIMEOUT = 10
MAX_WORKERS = 5
MAX_RETRIES = 3
MAX_CONTENT_SIZE = 1_000_000  # 1MB
CACHE_DURATION = timedelta(hours=1)

app = Flask(__name__)

# Кэш и блокировка
search_cache = {}
cache_lock = threading.Lock()

class SearchResult:
    def __init__(self, context, title, url, count):
        self.context = context
        self.title = title
        self.url = url
        self.count = count

def clean_old_cache():
    """Очистка устаревших результатов кэша"""
    current_time = datetime.now()
    with cache_lock:
        keys_to_remove = [
            key for key, (timestamp, _) in search_cache.items()
            if current_time - timestamp > CACHE_DURATION
        ]
        for key in keys_to_remove:
            del search_cache[key]
        gc.collect()

def rate_limit(seconds=1):
    """Декоратор для ограничения частоты запросов"""
    def decorator(f):
        last_request = {}
        
        @wraps(f)
        def decorated_function(*args, **kwargs):
            now = time()
            if f.__name__ in last_request:
                if now - last_request[f.__name__] < seconds:
                    return make_response('Too many requests', 429)
            last_request[f.__name__] = now
            return f(*args, **kwargs)
        return decorated_function
    return decorator

@lru_cache(maxsize=100)
def get_page_content(url):
    """Получение содержимого страницы с кэшированием"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Connection': 'close'
        }
        
        session = requests.Session()
        session.headers.update(headers)
        
        for _ in range(MAX_RETRIES):
            try:
                response = session.get(url, 
                                     timeout=REQUESTS_TIMEOUT, 
                                     verify=False,
                                     stream=True)
                response.raise_for_status()
                
                content = ''
                for chunk in response.iter_content(chunk_size=8192, decode_unicode=True):
                    content += chunk
                    if len(content) > MAX_CONTENT_SIZE:
                        break
                
                return content
            except requests.RequestException as e:
                logger.warning(f"Retry getting {url}: {str(e)}")
                continue
            finally:
                response.close()
                session.close()
        return None
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
        for script in soup(['script', 'style', 'meta', 'link']):
            script.decompose()
        
        text = soup.get_text()
        contexts = []
        pattern = f'.{{0,50}}{re.escape(search_term)}.{{0,50}}'
        matches = re.finditer(pattern, text, re.IGNORECASE)
        
        for match in matches:
            context = match.group().strip()
            if context not in contexts:
                contexts.append(context)
        
        return contexts
    except Exception as e:
        logger.error(f"Error extracting text: {str(e)}")
        return []

def search_page(url, search_term):
    """Поиск на странице"""
    try:
        content = get_page_content(url)
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

def get_links(url, max_depth):
    """Получение списка ссылок с сайта"""
    try:
        visited = set()
        to_visit = {url}
        base_domain = urlparse(url).netloc
        
        for _ in range(min(max_depth, 10)):
            current_urls = to_visit - visited
            if not current_urls:
                break
                
            for current_url in list(current_urls)[:10]:
                visited.add(current_url)
                content = get_page_content(current_url)
                if not content:
                    continue
                    
                try:
                    soup = BeautifulSoup(content, 'lxml')
                    for link in soup.find_all('a', href=True):
                        href = link.get('href')
                        if not href:
                            continue
                            
                        full_url = urljoin(current_url, href)
                        if not is_valid_url(full_url):
                            continue
                            
                        if urlparse(full_url).netloc != base_domain:
                            continue
                            
                        to_visit.add(full_url)
                        
                        if len(visited) >= max_depth:
                            return list(visited)
                except Exception as e:
                    logger.error(f"Error parsing links from {current_url}: {str(e)}")
                    continue
        
        return list(visited)
    except Exception as e:
        logger.error(f"Error in get_links: {str(e)}")
        return []

def cache_search_result(url, search_term, result):
    """Кэширование результатов поиска"""
    cache_key = f"{url}:{search_term}"
    with cache_lock:
        search_cache[cache_key] = (datetime.now(), result)

def get_cached_result(url, search_term):
    """Получение результатов из кэша"""
    cache_key = f"{url}:{search_term}"
    with cache_lock:
        if cache_key in search_cache:
            timestamp, result = search_cache[cache_key]
            if datetime.now() - timestamp <= CACHE_DURATION:
                return result
            del search_cache[cache_key]
    return None

@app.route('/favicon.ico')
def favicon():
    """Обработчик favicon.ico"""
    return send_from_directory(
        os.path.join(app.root_path, 'static'),
        'favicon.ico',
        mimetype='image/vnd.microsoft.icon'
    )

@app.route('/', methods=['GET', 'POST'])
@rate_limit(1)
def index():
    """Основной обработчик"""
    try:
        if request.method == 'POST':
            start_url = request.form['url'].strip()
            search_term = request.form['search_term'].strip()
            max_depth = min(int(request.form['max_depth']), 10)
            max_pages = min(int(request.form['max_pages']), 50)
            
            # Проверяем кэш
            cached_result = get_cached_result(start_url, search_term)
            if cached_result:
                logger.info("Returning cached result")
                return render_template('index.html', 
                                     results=cached_result,
                                     url=start_url,
                                     search_term=search_term,
                                     from_cache=True)
            
            logger.info(f"Starting search for term '{search_term}' on {start_url}")
            
            if not is_valid_url(start_url):
                return render_template('index.html', error="Некорректный URL")
            
            all_urls = get_links(start_url, max_pages)[:max_pages]
            
            if not all_urls:
                return render_template('index.html', 
                                     error="Не удалось получить страницы для поиска",
                                     url=start_url,
                                     search_term=search_term)
            
            results = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                future_to_url = {executor.submit(search_page, url, search_term): url 
                               for url in all_urls}
                
                for future in concurrent.futures.as_completed(future_to_url):
                    try:
                        result = future.result()
                        if result:
                            results.append(result)
                    except Exception as e:
                        logger.error(f"Error processing search result: {str(e)}")
            
            # Сортировка и удаление дубликатов
            unique_results = []
            seen_urls = set()
            for result in sorted(results, key=lambda x: x.count, reverse=True):
                if result.url not in seen_urls:
                    unique_results.append(result)
                    seen_urls.add(result.url)
            
            # Кэширование результатов
            cache_search_result(start_url, search_term, unique_results)
            
            logger.info(f"Search completed. Found {len(unique_results)} unique results")
            
            return render_template('index.html', 
                                 results=unique_results,
                                 url=start_url,
                                 search_term=search_term)
        
        return render_template('index.html')
        
    except Exception as e:
        logger.error(f"An error occurred: {str(e)}")
        return render_template('index.html', error="Произошла ошибка при выполнении поиска")

@app.errorhandler(429)
def ratelimit_handler(e):
    """Обработчик превышения лимита запросов"""
    return render_template('index.html', error="Слишком много запросов. Пожалуйста, подождите.")

@app.errorhandler(Exception)
def handle_exception(e):
    """Общий обработчик ошибок"""
    logger.error(f"Unhandled exception: {str(e)}")
    return render_template('index.html', error="Произошла непредвиденная ошибка")

# Инициализация планировщика для очистки кэша
scheduler = BackgroundScheduler()
scheduler.add_job(clean_old_cache, 'interval', minutes=30)
scheduler.start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
