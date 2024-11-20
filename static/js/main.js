// static/js/main.js

document.addEventListener('DOMContentLoaded', function() {
    // Инициализация при загрузке страницы
    initializeSearch();
    initializeFormValidation();
});

function initializeSearch() {
    const searchForm = document.querySelector('#searchForm');
    const loadingIndicator = document.querySelector('#loading');
    const resultsContainer = document.querySelector('#results');
    const searchButton = document.querySelector('#searchButton');

    if (searchForm) {
        searchForm.addEventListener('submit', function(e) {
            if (!validateForm()) {
                e.preventDefault();
                return false;
            }
            
            showLoading();
            disableSearchButton();
        });
    }

    // Подсветка результатов поиска
    highlightSearchResults();
}

function validateForm() {
    const url = document.querySelector('input[name="url"]').value;
    const searchTerm = document.querySelector('input[name="search_term"]').value;
    const maxDepth = document.querySelector('input[name="max_depth"]').value;
    const maxPages = document.querySelector('input[name="max_pages"]').value;

    // Проверка заполнения полей
    if (!url || !searchTerm || !maxDepth || !maxPages) {
        showError('Пожалуйста, заполните все поля');
        return false;
    }

    // Проверка URL
    try {
        new URL(url);
    } catch {
        showError('Пожалуйста, введите корректный URL');
        return false;
    }

    // Проверка числовых значений
    if (maxDepth < 1 || maxPages < 1) {
        showError('Значения должны быть больше 0');
        return false;
    }

    if (maxPages > 100) {
        showError('Максимальное количество страниц не может превышать 100');
        return false;
    }

    return true;
}

function showError(message) {
    const errorContainer = document.querySelector('#error');
    if (errorContainer) {
        errorContainer.textContent = message;
        errorContainer.style.display = 'block';
        setTimeout(() => {
            errorContainer.style.display = 'none';
        }, 3000);
    } else {
        alert(message);
    }
}

function showLoading() {
    const loading = document.querySelector('#loading');
    if (loading) {
        loading.style.display = 'block';
    }
}

function hideLoading() {
    const loading = document.querySelector('#loading');
    if (loading) {
        loading.style.display = 'none';
    }
}

function disableSearchButton() {
    const button = document.querySelector('#searchButton');
    if (button) {
        button.disabled = true;
    }
}

function enableSearchButton() {
    const button = document.querySelector('#searchButton');
    if (button) {
        button.disabled = false;
    }
}

function highlightSearchResults() {
    const searchTerm = document.querySelector('input[name="search_term"]').value;
    if (!searchTerm) return;

    const results = document.querySelectorAll('.result-context');
    results.forEach(result => {
        const text = result.innerHTML;
        const regex = new RegExp(searchTerm, 'gi');
        result.innerHTML = text.replace(regex, match => 
            `<span class="highlight">${match}</span>`
        );
    });
}

// Обработка ошибок AJAX
window.onerror = function(msg, url, lineNo, columnNo, error) {
    hideLoading();
    enableSearchButton();
    showError('Произошла ошибка при выполнении поиска');
    return false;
};
