# app.py
from flask import Flask, render_template, request, make_response
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from collections import Counter
import concurrent.futures
import re
import logging
from functools import wraps
from time import time

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

class SearchResult:
    def __init__(self, context, title, url, count):
        self.context = context
        self.title = title
        self.url = url
        self.count = count

def clean_text(text):
    """Очистка текста от лишних пробелов и специальных символов"""
    return ' '.join(text.split())

def sanitize_search_term(term):
    """Очистка поискового запроса от специальных символов"""
    return re.escape(term.strip())

def cache_result(func):
    """Декоратор для кэширования результатов поиска"""
    cache = {}
    
    @wraps(func)
    def wrapper(*args, **kwargs):
        key = str(args) + str(kwargs)
        if key in cache:
            return cache[key]
        result = func(*args, **kwargs)
        cache[key] = result
        return result
    return wrapper

def validate_input(url, search_term, max_depth, max_pages):
    """Валидация входных данных"""
    errors = []
    if not url:
        errors.append("URL не может быть пустым")
    if not search_term:
        errors.append("Поисковый запрос не может быть пустым")
    if max_depth < 1:
        errors.append("Глубина поиска должна быть больше 0")
    if max_pages < 1:
        errors.append("Количество страниц должно быть больше 0")
    if max_pages > 100:
        errors.append("Максимальное количество страниц не может превышать 100")
    return errors

def rate_limit(seconds=1):
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

def is_valid_url(url):
    try:
        result = urlparse(url)
        return all([result.scheme, result.netloc])
    except:
        logger.error(f"Invalid URL format: {url}")
        return False

@cache_result
def get_page_content(url):
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Connection': 'keep-alive',
        }
        response = requests.get(url, headers=headers, timeout=5)
        response.raise_for_status()
        return response.text
    except requests.RequestException as e:
        logger.error(f"Error fetching page {url}: {str(e)}")
        return None

def extract_text_with_context(html, search_term):
    try:
        soup = BeautifulSoup(html, 'html.parser')
        # Удаляем скрипты и стили
        for script in soup(["script", "style", "meta", "link"]):
            script.decompose()
        text = clean_text(soup.get_text())
        
        # Находим все вхождения искомого текста с контекстом
        contexts = []
        pattern = f'.{{0,50}}{search_term}.{{0,50}}'
        matches = re.finditer(pattern, text, re.IGNORECASE)
        
        for match in matches:
            context = clean_text(match.group())
            if context not in contexts:  # Избегаем дубликатов
                contexts.append(context)
        
        return contexts
    except Exception as e:
        logger.error(f"Error extracting text: {str(e)}")
        return []

@cache_result
def search_page(url, search_term):
    try:
        content = get_page_content(url)
        if not content:
            return None
        
        soup = BeautifulSoup(content, 'html.parser')
        title = soup.title.string if soup.title else url
        title = clean_text(title)
        
        contexts = extract_text_with_context(content, search_term)
        if not contexts:
            return None
        
        return SearchResult(
            context=contexts[0],
            title=title,
            url=url,
            count=len(contexts)
        )
    except Exception as e:
        logger.error(f"Error searching page {url}: {str(e)}")
        return None

def get_links(url, max_depth):
    try:
        visited = set()
        to_visit = {url}
        base_domain = urlparse(url).netloc
        
        for _ in range(max_depth):
            current_urls = to_visit - visited
            if not current_urls:
                break
                
            for current_url in current_urls:
                visited.add(current_url)
                content = get_page_content(current_url)
                if not content:
                    continue
                    
                soup = BeautifulSoup(content, 'html.parser')
                for link in soup.find_all('a'):
                    href = link.get('href')
                    if not href:
                        continue
                        
                    full_url = urljoin(current_url, href)
                    if not is_valid_url(full_url):
                        continue
                        
                    if urlparse(full_url).netloc != base_domain:
                        continue
                        
                    to_visit.add(full_url)
                    
                    if len(visited) >= int(max_depth):
                        return list(visited)
        
        return list(visited)
    except Exception as e:
        logger.error(f"Error getting links from {url}: {str(e)}")
        return []

@app.route('/', methods=['GET', 'POST'])
@rate_limit(1)
def index():
    try:
        if request.method == 'POST':
            start_url = request.form['url'].strip()
            search_term = request.form['search_term'].strip()
            try:
                max_depth = int(request.form['max_depth'])
                max_pages = int(request.form['max_pages'])
            except ValueError:
                return render_template('index.html', error="Некорректные числовые значения")

            # Валидация входных данных
            errors = validate_input(start_url, search_term, max_depth, max_pages)
            if errors:
                return render_template('index.html', error=", ".join(errors))

            logger.info(f"Starting search for term '{search_term}' on {start_url}")
            
            if not is_valid_url(start_url):
                logger.warning(f"Invalid URL provided: {start_url}")
                return render_template('index.html', error="Некорректный URL")
            
            # Очистка поискового запроса
            search_term = sanitize_search_term(search_term)
            
            # Получаем все ссылки для поиска
            all_urls = get_links(start_url, max_pages)[:max_pages]
            
            if not all_urls:
                logger.warning(f"No URLs found to search")
                return render_template('index.html', error="Не удалось найти страницы для поиска")
            
            # Выполняем поиск параллельно
            results = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                future_to_url = {executor.submit(search_page, url, search_term): url 
                               for url in all_urls}
                
                for future in concurrent.futures.as_completed(future_to_url):
                    try:
                        result = future.result()
                        if result:
                            results.append(result)
                    except Exception as e:
                        logger.error(f"Error processing search result: {str(e)}")
            
            # Удаляем дубликаты и сортируем результаты
            unique_results = []
            seen_urls = set()
            for result in sorted(results, key=lambda x: x.count, reverse=True):
                if result.url not in seen_urls:
                    unique_results.append(result)
                    seen_urls.add(result.url)
            
            logger.info(f"Search completed. Found {len(unique_results)} unique results")
            
            return render_template('index.html', 
                                 results=unique_results,
                                 search_term=search_term,
                                 url=start_url)
        
        return render_template('index.html')
        
    except Exception as e:
        logger.error(f"An error occurred: {str(e)}")
        return render_template('index.html', error="Произошла ошибка при выполнении поиска")

@app.errorhandler(429)
def ratelimit_handler(e):
    return render_template('index.html', error="Слишком много запросов. Пожалуйста, подождите.")

@app.errorhandler(Exception)
def handle_exception(e):
    logger.error(f"Unhandled exception: {str(e)}")
    return render_template('index.html', error="Произошла непредвиденная ошибка")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
