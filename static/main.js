/**
 * VK IP Panel — общие JS-функции.
 * Подключается во всех шаблонах через <script src="/static/main.js">.
 */

/**
 * Форматирует дату в "X мин назад" и т.д.
 * @param {string} dateStr - дата в формате "YYYY-MM-DD HH:MM:SS"
 * @returns {string}
 */
function timeAgo(dateStr) {
    if (!dateStr) return 'Ещё не обновлялось';

    // Поддержка и "YYYY-MM-DD HH:MM:SS" и "YYYY-MM-DDTHH:MM:SSZ"
    let d = dateStr.includes('T') ? dateStr : dateStr.replace(' ', 'T') + 'Z';
    const date = new Date(d);
    const now = new Date();
    const seconds = Math.floor((now - date) / 1000);

    if (seconds < 0) return 'только что';
    if (seconds < 60) return 'только что';
    if (seconds < 120) return '1 мин назад';
    if (seconds < 3600) return Math.floor(seconds / 60) + ' мин назад';
    if (seconds < 7200) return '1 час назад';
    if (seconds < 86400) return Math.floor(seconds / 3600) + ' ч назад';
    return Math.floor(seconds / 86400) + ' дн назад';
}

/**
 * Показать toast-уведомление.
 * @param {string} message - текст
 * @param {string} type - 'success' | 'error' | 'info'
 * @param {number} duration - длительность в мс (по умолчанию 3000)
 */
function showToast(message, type = 'info', duration = 3000) {
    const container = document.getElementById('toast-container');
    if (!container) return;

    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;

    const icons = { success: '✓', error: '✕', info: 'ℹ' };
    toast.innerHTML = `<span>${icons[type] || ''}</span> ${message}`;
    container.appendChild(toast);

    setTimeout(() => {
        toast.style.animation = 'slideOut 0.3s ease forwards';
        setTimeout(() => toast.remove(), 300);
    }, duration);
}

/**
 * Обновить элемент #last-update с относительным временем.
 * Ищет элемент с id="last-update" и атрибутом data-time.
 */
function updateTimeAgo() {
    const el = document.getElementById('last-update');
    if (!el) return;
    const time = el.dataset.time;
    if (time) {
        el.textContent = 'Обновлено: ' + timeAgo(time);
    }
}

/**
 * AJAX-запрос с заголовком XMLHttpRequest.
 * @param {string} url
 * @param {string} method - HTTP метод
 * @param {object|null} data - тело запроса (JSON)
 * @returns {Promise<object>}
 */
async function apiRequest(url, method = 'GET', data = null) {
    const options = {
        method,
        headers: { 'X-Requested-With': 'XMLHttpRequest' },
        credentials: 'same-origin',
    };

    if (data) {
        options.headers['Content-Type'] = 'application/json';
        options.body = JSON.stringify(data);
    }

    const res = await fetch(url, options);
    return res.json();
}

// Автозапуск: обновляем timeAgo при загрузке и каждую минуту
document.addEventListener('DOMContentLoaded', () => {
    updateTimeAgo();
    setInterval(updateTimeAgo, 60000);
});
