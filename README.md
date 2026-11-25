# Omni Shopping Agent

Мультиканальна інженерна система для пошуку, перевірки та порівняння товарів в Україні, ЄС, США та на AliExpress із точним розрахунком повної вартості доставки й розмитнення (`Landed Cost`), 3-етапною статистичною воронкою (`IQR` + виявлення пасток), багатокритеріальним ранжуванням `Pareto / TOPSIS` та веб-порталом прийняття рішень (`HITL Portal`).

---

## Основні можливості

- **Український локальний + Б/В / Open-Box режим (`--ua-only --include-used`):**
  - Фокус на українських каналах (`Hotline`, `Rozetka`, `Prom`, `OLX`) без зайвого закордонного імпорту для крихкої техніки (наприклад, 42–43" моніторів, де потрібен живий огляд пікселів і матриці).
  - Автоматичне додавання станів `USED_GOOD`, `OPEN_BOX`, `REFURBISHED` та генерація прямих smart-посилань для ручної перевірки:
    - **OLX Київ / Україна** (б/в та нове з фільтром за ціною);
    - **Prom.ua** (б/в та уцінка з Prom-оплатою);
    - **Rozetka Уцінка / Вітрина**;
    - **Telemart** (Factory Refurbished / Уцінка);
    - **CompBest** (перевірені б/в монітори з Європи);
    - **Hotline.ua** та **IZI / Shafa**.
- **Глобальний режим (UA + EU + AliExpress + Deep-Links):**
  - Amazon DE/PL/FR/IT/ES (подвійний маршрут: прямий експорт `-19% VAT` vs форвардер `NP Shopping / Meest`), `Idealo.de`, `Ceneo.pl`, `AliExpress` (з урахуванням купонів та `TelegramPromoScraper`), deep-links для `Allegro.pl`, `Alza.cz`, `Heureka.cz`, `Amazon US`, `eBay US`.
  - Автоматичний розрахунок митного ліміту `150 EUR` (`10% мито + 20% ПДВ`) або пільги `0%` (`energy_exempt` для зарядних станцій).
- **Fail-Closed Compliance (нуль обходу капч):**
  - При `403/429/503` або виявленні Cloudflare / AWS WAF / CAPTCHA конектор миттєво повертає `MANUAL_CHECK_LINK` з точним розрахунком **точки беззбитковості (breakeven price)**.
- **3-етапна воронка + Pareto/TOPSIS:**
  - Відсіює кабелі/кронштейни, биті матриці (`for_parts`), пастки мульти-SKU та `kit traps` (`Solo / Ohne Akku / 110V`).
  - Будує **Top-Chart Quartet**, **Hard Trade-Off Cards** (наприклад, офіційний LG IPS vs вживаний на OLX або Open-Box) та інтерактивний HTML-звіт зі слайдерами пріоритетів.
- **Синхронізація та бекапи:**
  - SQLite (`data/shopping_hub.db`) зберігає історію цін, алерти зниження цін і людські рішення (`APPROVE / WATCH / REJECT`), які **не перезатираються** при повторному парсингу.
  - Атомарний бекап SQLite на Google Drive (`sqlite3.Connection.backup()`) та інтеграція з Google Sheets (`google_apps_script/Code.gs`, колонки `A:V` оновлюються, а людські `W:Y` суворо зберігаються).

---

## Швидкий старт

### 1. Встановлення залежностей (через `uv`)

```bash
uv sync --extra dev
```

### 2. Офлайн демо-запуск (перевірка без інтернету)

Локальний український пошук моніторів 43" з б/в та Open-Box (LG IPS vs Samsung VA):

```bash
uv run omni-agent search "Монітор 43 4K IPS LG 43SQ700S-W 43UN700-B" --ua-only --include-used --demo --budget 25000 --weight-kg 11.5
```

Результат буде збережено у:
- `reports/latest_report.html` — самодостатній інтерактивний HITL-звіт (можна відкрити у будь-якому браузері без сервера);
- `reports/<task_id>_briefing.md` — стислий Markdown-брифінг;
- `data/shopping_hub.db` — локальна SQLite-база.

### 3. Живий пошук по Україні або глобально

```bash
# Лише Україна + б/в / Open-Box (ідеально для великих моніторів із живим оглядом)
uv run omni-agent search "LG 43SQ700S-W" --ua-only --include-used --weight-kg 11.5

# Повний глобальний пошук (UA + EU + AliExpress + Deep-Links)
uv run omni-agent search "EcoFlow DELTA 2 Max" --category power_stations --weight-kg 23 --customs-mode energy_exempt
```

### 4. Черга задач (`run-queue`) та локальний веб-портал (`serve`)

```bash
# Виконати задачі з JSON-файлу (або з Google Sheets за наявності вебхука)
uv run omni-agent run-queue --tasks-json tasks.json --ua-only --include-used --export-dir reports

# Запустити локальний FastAPI HITL Portal (http://127.0.0.1:8765)
uv run omni-agent serve --host 127.0.0.1 --port 8765
```

### 5. Запуск на Windows (`.bat`)

- `run_harvester.bat` — перевіряє `uv`, оновлює репозиторій (`git pull --ff-only`), запускає `omni-agent run-queue` і відкриває `reports\latest_report.html` (передайте `--silent` для фонового запуску).
- `run_portal.bat` — запускає FastAPI HITL Portal на `http://127.0.0.1:8765`.

---

## Налаштування Google Sheets & Google Drive

1. Створіть нову Google-таблицю та відкрийте **Extensions → Apps Script**.
2. Скопіюйте туди вміст `google_apps_script/Code.gs` та `google_apps_script/appsscript.json`.
3. Виконайте функцію `setupMasterSpreadsheet()` — вона створить 4 вкладки:
   - `1_Task_Queue`
   - `2_Top_Charts_And_Jackpots` (колонки `A:V` для агента, `W:Y` для ваших рішень)
   - `3_Manual_Check_Links`
   - `4_Funnel_Audit_And_Quarantine`
4. Опублікуйте скрипт як **Web App** (Execute as: Me, Access: Anyone) та задайте змінні середовища:
   ```bash
   export OMNI_GSHEETS_WEBAPP_URL="https://script.google.com/macros/s/.../exec"
   export OMNI_GSHEETS_SYNC_SECRET="your-optional-secret"
   export OMNI_DRIVE_BACKUP_DIR="/path/to/GoogleDrive/OmniShoppingBackups"
   ```
5. (Опціонально) Для AI-резюме через Gemini 2.5 Flash задайте `GEMINI_API_KEY`. Якщо ключ не задано, агент працює за 0 грн у детермінованому локальному режимі.

---

## Тестування

```bash
uv run pytest -v
```
