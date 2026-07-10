import csv
import re
import random
import os
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

# подмена признаков автоматизации, чтоб яндекс не сразу палил headless/playwright
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['ru-RU', 'ru', 'en-US'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
window.chrome = { runtime: {} };
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : originalQuery(parameters)
);
"""

PROFILE_DIR = "./browser_profile"  # тут будут куки и т.п., чтоб не логиниться каждый раз заново

CITIES = ["минск", "брест", "гродно", "могилев", "витебск", "гомель", "барановичи"]
# можно добавить остальные, но пока хватает и этих
# "бобруйск", "барановичи", "пинск", "орша", "мозырь", "солигорск"

QUERY_TEMPLATES = ["купить цветы {city}"]
# "доставка цветов {city}", "заказать цветы {city}", "интернет магазин цветов {city}"

SEARCH_QUERIES = [tpl.format(city=c) for c in CITIES for tpl in QUERY_TEMPLATES]

PAGES_PER_QUERY = 2  # больше 1-2 лучше не ставить, капча вылезает быстро

IGNORE_DOMAINS = [
    'yandex', 'google', 'instagram', 'vk.com', 'facebook', 'youtube',
    'by.wildberries', 'ozon', 'kufar.by', 'deal.by', 'tam.by', 'relax.by',
    '103.by', 'onliner.by', 'telegram', 't.me', 'whatsapp', 'twitter.com'
]

CONTACT_KEYWORDS = ['contact', 'kontakt', 'about', 'o-nas', 'o_nas', 'company']

PHONE_RE = r'(?:\+375|375|8\s?0)(?:\s?\(?\d{2}\)?[\s\-]?)\d{3}[\s\-]?\d{2}[\s\-]?\d{2}'
EMAIL_RE = r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+'

OUTPUT_CSV = 'auto_flower_contacts.csv'


def pause(a=1.0, b=3.0):
    # рандомная пауза, иногда специально затягиваем подольше - будто человек отвлекся
    t = random.uniform(a, b)
    if random.random() < 0.15:
        t += random.uniform(2, 5)
    return t


def long_break_needed(i, every=4):
    if i > 0 and i % every == 0:
        t = random.uniform(60, 180)
        print(f"перерыв на {int(t)} сек, чтоб не спалиться перед яндексом...")
        return t
    return 0


def act_like_human(page):
    try:
        w = page.viewport_size['width']
        h = page.viewport_size['height']
        for _ in range(random.randint(2, 4)):
            page.mouse.move(random.randint(0, w), random.randint(0, h), steps=random.randint(5, 15))
            page.wait_for_timeout(random.randint(100, 400))
        page.mouse.wheel(0, random.randint(200, 800))
        page.wait_for_timeout(random.randint(300, 700))
    except Exception:
        pass


def decode_domain(domain):
    # punycode (xn--...) превращаем обратно в читаемый вид, например
    # xn--c1ac1acci.xn--g1anf0c.xn--90ais -> гродно.розы.бел
    parts = domain.split('.')
    result = []
    for part in parts:
        if part.startswith('xn--'):
            try:
                part = part.encode('ascii').decode('idna')
            except Exception:
                pass  # не смогли раскодировать - оставляем как было
        result.append(part)
    return '.'.join(result)


def collect_links_for_query(page, query):
    found = set()

    for p_num in range(PAGES_PER_QUERY + 1):
        url = f"https://yandex.by/search/?text={query}&p={p_num}"
        try:
            # ждём только готовности разметки (domcontentloaded), а не полной
            # загрузки страницы (load) - у яндекса куча фоновых запросов
            page.goto(url, timeout=30000, wait_until='domcontentloaded')

            if "smartcaptcha" in page.content().lower() or page.locator('form[action*="checkcaptcha"]').count() > 0:
                print("капча от яндекса, реши руками и подожди пока выдача не появится...")
                page.wait_for_selector('#search-result', timeout=120000)
                print("капча пройдена")
            else:
                page.wait_for_selector('#search-result', timeout=15000)

            page.wait_for_timeout(int(pause(1.5, 3.0) * 1000))
            act_like_human(page)

            for link in page.locator('a').all():
                href = link.get_attribute('href')
                if not href or not href.startswith('http'):
                    continue
                parsed = urlparse(href)
                domain = parsed.netloc.replace('www.', '').lower()
                if not domain or any(bad in domain for bad in IGNORE_DOMAINS):
                    continue
                # в found храним только сам домен, без схемы (http/https) -
                # так один и тот же сайт не попадёт дважды из-за разных ссылок на него
                found.add(domain)

        except Exception as e:
            print(f"страница {p_num} по запросу '{query}' не открылась: {e}")
            continue

    print(f"'{query}' -> найдено сайтов: {len(found)}")
    return found


def normalize_phone(raw):
    # приводим любой номер к единому виду: 375XXYYYYYYY (12 цифр, без + и пробелов)
    if not raw:
        return None

    digits = re.sub(r'\D', '', raw)

    if digits.startswith('375') and len(digits) == 12:
        return digits

    if digits.startswith('80') and len(digits) == 11:
        return '375' + digits[2:]

    if digits.startswith('0') and len(digits) == 10:
        return '375' + digits[1:]

    if len(digits) == 9:
        return '375' + digits

    return None  # не похоже на нормальный белорусский номер - пропускаем


def find_all_phones(page):
    # собираем ВСЕ номера на странице, а не только первый попавшийся
    phones = []
    seen = set()

    try:
        for link in page.locator('a[href^="tel:"]').all():
            href = link.get_attribute('href')
            if not href:
                continue
            phone = normalize_phone(href.replace('tel:', ''))
            if phone and phone not in seen:
                phones.append(phone)
                seen.add(phone)
    except Exception:
        pass

    try:
        html = page.content()
        for raw in re.findall(PHONE_RE, html):
            phone = normalize_phone(raw)
            if phone and phone not in seen:
                phones.append(phone)
                seen.add(phone)
    except Exception:
        pass

    return phones


def find_email(page):
    email = None

    try:
        mail_links = page.locator('a[href^="mailto:"]').all()
        if mail_links:
            email = mail_links[0].get_attribute('href').replace('mailto:', '').strip()
    except Exception:
        pass

    if not email:
        try:
            html = page.content()
            m = re.search(EMAIL_RE, html)
            if m:
                email = m.group(0).strip()
        except Exception:
            pass

    return email


def find_contacts_link(page):
    try:
        for link in page.locator('a').all():
            href = link.get_attribute('href')
            if href and any(kw in href.lower() for kw in CONTACT_KEYWORDS):
                return href
    except Exception:
        pass
    return None


def get_contacts(page, base_url):
    phones = find_all_phones(page)
    email = find_email(page)

    if phones and email:
        return phones, email

    link = find_contacts_link(page)
    if not link:
        return phones, email

    try:
        target = link if link.startswith('http') else base_url.rstrip('/') + '/' + link.lstrip('/')
        page.goto(target, timeout=15000, wait_until='domcontentloaded')
        page.wait_for_timeout(1500)

        for phone in find_all_phones(page):
            if phone not in phones:
                phones.append(phone)

        if not email:
            email = find_email(page)
    except Exception:
        pass

    return phones, email


def append_row(filename, row, header=False):
    mode = 'w' if header else 'a'
    with open(filename, mode=mode, encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['Сайт', 'Телефоны', 'Email'], delimiter=';')
        if header:
            w.writeheader()
        if row is not None:
            w.writerow(row)


def main():
    sites = set()

    with sync_playwright() as p:
        # постоянный профиль - куки и локальное хранилище сохраняются между запусками,
        # ведет себя больше как обычный челк, а не голый запуск браузера каждый раз
        context = p.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=False,
            viewport={'width': 1366, 'height': 768},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            permissions=['geolocation', 'notifications'],
            locale='ru-RU',
            timezone_id='Europe/Minsk',
            args=['--disable-blink-features=AutomationControlled'],
        )
        context.add_init_script(STEALTH_JS)
        page = context.new_page()

        print("этап 1 - собираю сайты из выдачи")
        for i, q in enumerate(SEARCH_QUERIES):
            print(f"запрос: {q}")
            sites |= collect_links_for_query(page, q)

            page.wait_for_timeout(int(pause(8, 15) * 1000))
            extra = long_break_needed(i)
            if extra:
                page.wait_for_timeout(int(extra * 1000))

        print(f"всего уникальных сайтов: {len(sites)}")

        if not sites:
            print("пусто. видимо выдача после капчи не подгрузилась")
            context.close()
            return

        print("этап 2 - собираю телефоны и почты")
        append_row(OUTPUT_CSV, None, header=True)

        for i, domain in enumerate(sites, start=1):
            url = "https://" + domain
            display_site = "https://" + decode_domain(domain)  # для отчёта показываем в читаемом виде
            print(f"[{i}/{len(sites)}] {display_site}")

            try:
                response = page.goto(url, timeout=20000, wait_until='domcontentloaded')

                # сайт не открылся или отдал ошибку - помечаем как мёртвый и не тратим на него время
                if response is None or response.status >= 400:
                    row = {'Сайт': display_site, 'Телефоны': 'Сайт недоступен', 'Email': 'Сайт недоступен'}
                    append_row(OUTPUT_CSV, row)
                    print("  -> сайт недоступен")
                    page.wait_for_timeout(int(pause(1, 3) * 1000))
                    continue

                page.wait_for_timeout(1500)
                phones, email = get_contacts(page, url)

                phones_text = ", ".join(phones) if phones else "Не найден"
                row = {'Сайт': display_site, 'Телефоны': phones_text, 'Email': email or "Не найден"}
                print(f"  -> тел: {phones_text}, email: {email or 'Не найден'}")

            except Exception:
                row = {'Сайт': display_site, 'Телефоны': "Ошибка загрузки", 'Email': "Ошибка загрузки"}
                print("  -> не открылся")

            append_row(OUTPUT_CSV, row)
            page.wait_for_timeout(int(pause(1, 3) * 1000))

        context.close()

    print(f"готово, результаты в '{OUTPUT_CSV}'")


if __name__ == '__main__':
    main()